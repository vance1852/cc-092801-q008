from __future__ import annotations

import json
import threading
import unittest
import urllib.request
from datetime import datetime, timedelta
from http.server import ThreadingHTTPServer

from licensing_ops.termination import (
    InvalidCaseState,
    PrerequisiteError,
    ScopeItemInput,
    ScopeReferenceError,
    TerminationError,
    add_days,
    clearance_blockers,
    close_blockers,
    is_at_or_after,
    notice_fingerprint,
)
from licensing_ops.termination import CaseSnapshot
from licensing_ops.termination_api import Handler, bootstrap
from licensing_ops.termination_service import TerminationService


class MutableClock:
    def __init__(self, start: str) -> None:
        self.at = datetime.fromisoformat(start)

    def __call__(self) -> str:
        return self.at.isoformat()

    def advance(self, **kwargs) -> None:
        self.at += timedelta(**kwargs)


def right(region="CN", seq=0, clause="Original §3", disposition="revert"):
    return ScopeItemInput("right", region, f"{region} 权利", disposition, seq, clause)


class TerminationServiceTestBase(unittest.TestCase):
    def setUp(self) -> None:
        self.clock = MutableClock("2026-10-02T09:00:00+00:00")
        self.svc = TerminationService(":memory:", clock=self.clock)
        for uid, pwd, role in (
            ("legal", "legal-pass-2026", "legal"),
            ("rd", "rd-pass-2026", "rd"),
            ("bd", "bd-pass-2026", "bd"),
            ("auditor", "auditor-2026", "auditor"),
        ):
            self.svc.auth.create_user(uid, pwd, role)
        self.legal = self.svc.auth.login("legal", "legal-pass-2026")
        self.rd = self.svc.auth.login("rd", "rd-pass-2026")
        self.bd = self.svc.auth.login("bd", "bd-pass-2026")
        self.auditor = self.svc.auth.login("auditor", "auditor-2026")
        self.svc.register_agreement(
            self.legal, "AGR-1", "全球开发授权", "AcmeBio", "2023-01-01T00:00:00+00:00")

    def open_case(self, notice_key="N-1", days=30):
        result = self.svc.open_termination_case(
            self.legal, "AGR-1", "终止案", "strategic_adjustment", "战略调整",
            notice_key, "2026-10-01T00:00:00+00:00", days)
        return result["case"]["case_id"]

    def finalize(self, case_id, items=()):
        for item in items:
            self.svc.add_scope_item(self.legal, case_id, item)
        self.clock.advance(days=31)
        return self.svc.finalize_scope(self.legal, case_id)


class AgreementTests(TerminationServiceTestBase):
    def test_amendments_must_be_sequential(self) -> None:
        self.svc.add_amendment(self.legal, "AGR-1", 1, "修订一",
                               "2024-01-01T00:00:00+00:00", "EU")
        with self.assertRaises(ScopeReferenceError):
            self.svc.add_amendment(self.legal, "AGR-1", 3, "修订三",
                                   "2025-01-01T00:00:00+00:00", "跳号")

    def test_scope_must_reference_registered_amendment(self) -> None:
        self.svc.add_amendment(self.legal, "AGR-1", 1, "修订一",
                               "2024-01-01T00:00:00+00:00", "EU")
        case_id = self.open_case()
        with self.assertRaises(ScopeReferenceError):
            self.svc.add_scope_item(self.legal, case_id,
                                    ScopeItemInput("right", "JP", "JP 权利", "revert", 5, "Am5 §1"))
        # seq=0 永远指向原协议，合法
        added = self.svc.add_scope_item(self.legal, case_id, right())
        self.assertEqual(added["source_amendment_seq"], 0)

    def test_scope_item_validates_disposition_per_type(self) -> None:
        with self.assertRaises(TerminationError):
            ScopeItemInput("right", "CN", "x", "ongoing", 0, "c").validate()
        with self.assertRaises(TerminationError):
            ScopeItemInput("obligation", "CN", "x", "revert", 0, "c").validate()
        # 共同开发允许只暂停
        ScopeItemInput("collaboration", "CN", "x", "suspend", 0, "c").validate()


class NoticeDedupTests(TerminationServiceTestBase):
    def test_same_notice_is_idempotent(self) -> None:
        first = self.svc.open_termination_case(
            self.legal, "AGR-1", "t", "clinical_outcome", "临床失败",
            "N-DUP", "2026-10-01T00:00:00+00:00", 10)
        second = self.svc.open_termination_case(
            self.legal, "AGR-1", "t", "clinical_outcome", "临床失败",
            "N-DUP", "2026-10-05T00:00:00+00:00", 10)
        self.assertFalse(first["duplicate"])
        self.assertTrue(second["duplicate"])
        self.assertEqual(first["case"]["case_id"], second["case"]["case_id"])
        count = self.svc.db.execute("SELECT count(*) FROM term_cases").fetchone()[0]
        self.assertEqual(count, 1)
        types = [e["event_type"] for e in self.svc.timeline(self.legal, first["case"]["case_id"])["events"]]
        self.assertIn("notice.repeated", types)

    def test_new_notice_while_open_does_not_start_second_reversion(self) -> None:
        c1 = self.open_case("N-A")
        c2 = self.open_case("N-B")
        self.assertEqual(c1, c2)

    def test_notice_after_close_never_starts_second_reversion(self) -> None:
        case_id = self.open_case()
        self.finalize(case_id, [right()])
        item = self.svc.case(self.legal, case_id)["items"][0]
        self.svc.start_handover(self.rd, case_id, item["item_id"])
        self.svc.complete_handover(self.rd, case_id, item["item_id"], "DEED-1")
        self.svc.close_case(self.legal, case_id, "完成")
        after = self.svc.open_termination_case(
            self.legal, "AGR-1", "t", "mutual", "新情况", "N-AFTER",
            "2026-12-01T00:00:00+00:00", 30)
        self.assertTrue(after["duplicate"])
        self.assertEqual(after["case"]["case_id"], case_id)
        self.assertEqual(self.svc.db.execute("SELECT count(*) FROM term_cases").fetchone()[0], 1)

    def test_fingerprint_stable_and_distinct(self) -> None:
        self.assertEqual(notice_fingerprint("a", "n1"), notice_fingerprint("a", "n1"))
        self.assertNotEqual(notice_fingerprint("a", "n1"), notice_fingerprint("a", "n2"))


class DisputeTests(TerminationServiceTestBase):
    def test_dispute_lifecycle_blocks_finalize(self) -> None:
        case_id = self.open_case(days=30)
        self.svc.add_scope_item(self.legal, case_id, right())
        dispute = self.svc.raise_dispute(self.legal, case_id, "数据归属", "有异议")
        self.assertEqual(self.svc.case(self.legal, case_id)["status"], "disputed")
        with self.assertRaises(InvalidCaseState):
            self.svc.finalize_scope(self.legal, case_id)
        self.svc.resolve_dispute(self.legal, dispute["dispute_id"], "按条款回转")
        self.clock.advance(days=31)
        self.assertEqual(self.svc.finalize_scope(self.legal, case_id)["status"], "scope_finalized")

    def test_dispute_after_window_rejected(self) -> None:
        case_id = self.open_case(days=10)
        self.clock.advance(days=11)
        with self.assertRaises(InvalidCaseState):
            self.svc.raise_dispute(self.legal, case_id, "迟到争议", "x")

    def test_scope_locked_after_finalize(self) -> None:
        case_id = self.open_case()
        self.finalize(case_id, [right()])
        with self.assertRaises(InvalidCaseState):
            self.svc.add_scope_item(self.legal, case_id, ScopeItemInput(
                "data", "global", "补充数据", "revert", 0, "Original §8"))


class HandoverAndSuspensionTests(TerminationServiceTestBase):
    def _finalized_case_with(self, *items):
        case_id = self.open_case()
        self.finalize(case_id, list(items))
        return case_id

    def test_unfinished_handover_blocks_close(self) -> None:
        case_id = self._finalized_case_with(right())
        item = self.svc.case(self.legal, case_id)["items"][0]
        self.svc.start_handover(self.rd, case_id, item["item_id"])
        with self.assertRaises(PrerequisiteError) as ctx:
            self.svc.close_case(self.legal, case_id, "提前关闭")
        self.assertIn("handover_unconfirmed", str(ctx.exception))

    def test_complete_handover_requires_evidence(self) -> None:
        case_id = self._finalized_case_with(right())
        item = self.svc.case(self.legal, case_id)["items"][0]
        self.svc.start_handover(self.rd, case_id, item["item_id"])
        with self.assertRaises(TerminationError):
            self.svc.complete_handover(self.rd, case_id, item["item_id"], "  ")

    def test_only_affected_part_of_collaboration_suspended(self) -> None:
        collab = ScopeItemInput("collaboration", "global", "共同开发 II 期", "suspend",
                                0, "Original §6", detail={"affected_part": "队列A"})
        case_id = self._finalized_case_with(
            ScopeItemInput("right", "CN", "CN 权利", "revert", 0, "§3"), collab)
        items = {i["subject"]: i for i in self.svc.case(self.legal, case_id)["items"]}
        # 共同开发先暂停
        suspended = self.svc.suspend_affected_part(
            self.legal, case_id, items["共同开发 II 期"]["item_id"], "仅暂停队列A")
        self.assertEqual(suspended["status"], "suspended")
        # 暂停但未拿到解除/继续结论时不能关闭
        with self.assertRaises(PrerequisiteError) as ctx:
            self.svc.close_case(self.legal, case_id, "尝试关闭")
        self.assertIn("collaboration_suspension_unresolved", str(ctx.exception))
        # 第三方解除函到达 → 解除，案件可关闭；未受影响部分始终未被终止
        self.svc.resolve_collaboration(
            self.legal, case_id, items["共同开发 II 期"]["item_id"],
            "released", "CRO-RELEASE-1", "队列B联合研究按原协议继续")
        # 权利项仍需完成
        self.svc.start_handover(self.rd, case_id, items["CN 权利"]["item_id"])
        self.svc.complete_handover(self.rd, case_id, items["CN 权利"]["item_id"], "DEED")
        closed = self.svc.close_case(self.legal, case_id, "完成")
        self.assertEqual(closed["status"], "closed")

    def test_suspended_sublicense_encumbers_relicense_but_not_self_research(self) -> None:
        sub = ScopeItemInput("sublicense", "EU", "EuroLab 再许可", "suspend", 0, "§5")
        case_id = self._finalized_case_with(
            ScopeItemInput("right", "EU", "EU 权利", "revert", 0, "§3"), sub)
        items = {i["subject"]: i for i in self.svc.case(self.legal, case_id)["items"]}
        self.svc.suspend_affected_part(self.legal, case_id, items["EuroLab 再许可"]["item_id"],
                                       "暂停新适应症")
        self.svc.start_handover(self.rd, case_id, items["EU 权利"]["item_id"])
        self.svc.complete_handover(self.rd, case_id, items["EU 权利"]["item_id"], "EU-DEED")
        # 再许可维持暂停也可以关闭（暂停被视为对方暂不行使，权利负担仍在）
        closed = self.svc.close_case(self.legal, case_id, "完成，再许可维持暂停")
        self.assertEqual(closed["status"], "closed")
        resume = self.svc.request_clearance(self.legal, case_id, "resume_self_research")
        self.assertEqual(resume["decision"], "granted")
        with self.assertRaises(PrerequisiteError) as ctx:
            self.svc.request_clearance(self.legal, case_id, "relicense", region="EU")
        self.assertIn("suspended_sublicense_encumbers_region", str(ctx.exception))
        # 拒绝决定本身必须落库留痕（事务提交后才抛出）
        rejected_count = self.svc.db.execute(
            "SELECT count(*) AS n FROM term_clearances WHERE case_id=? AND kind='relicense' "
            "AND decision='rejected'",
            (case_id,)).fetchone()["n"]
        self.assertGreaterEqual(rejected_count, 1)
        # 正式终止再许可后，重新授权放行通过
        self.svc.resolve_sublicense(self.legal, case_id, items["EuroLab 再许可"]["item_id"],
                                    "terminated", "SUB-NOTICE-9")
        granted = self.svc.request_clearance(self.legal, case_id, "relicense", region="EU")
        self.assertEqual(granted["decision"], "granted")


class CloseAndLateMaterialTests(TerminationServiceTestBase):
    def _closed_case(self):
        case_id = self.open_case()
        self.finalize(case_id, [
            ScopeItemInput("right", "CN", "CN 权利", "revert", 0, "§3"),
            ScopeItemInput("data", "global", "数据包", "revert", 0, "§8"),
            ScopeItemInput("obligation", "global", "患者随访", "ongoing", 0, "§12"),
        ])
        items = self.svc.case(self.legal, case_id)["items"]
        by_type = {item["item_type"]: item for item in items}
        for item_type, evidence in (("right", "DEED"), ("data", "MANIFEST")):
            item = by_type[item_type]
            self.svc.start_handover(self.rd, case_id, item["item_id"])
            self.svc.complete_handover(self.rd, case_id, item["item_id"], evidence)
        self.svc.close_case(self.legal, case_id, "权利回转完成，随访义务持续")
        return case_id, items

    def test_late_material_is_logged_but_never_reopens(self) -> None:
        case_id, _ = self._closed_case()
        result = self.svc.record_late_material(
            self.rd, case_id, "data_correction", "EMAIL-42", "迟到的数据更正",
            received_at="2026-12-15T00:00:00+00:00")
        self.assertFalse(result["case_reopened"])
        self.assertEqual(result["case_status"], "closed")
        self.assertEqual(self.svc.case(self.legal, case_id)["status"], "closed")
        # 迟到材料后仍不允许任何状态变更
        data_item = next(i for i in self.svc.case(self.legal, case_id)["items"]
                         if i["item_type"] == "data")
        with self.assertRaises(InvalidCaseState):
            self.svc.start_handover(self.rd, case_id, data_item["item_id"])
        with self.assertRaises(InvalidCaseState):
            self.svc.close_case(self.legal, case_id, "再次关闭")
        logged = self.svc.case(self.legal, case_id)["late_materials"]
        self.assertEqual(len(logged), 1)

    def test_clearance_requires_closed_case(self) -> None:
        case_id = self.open_case()
        with self.assertRaises(PrerequisiteError) as ctx:
            self.svc.request_clearance(self.legal, case_id, "resume_self_research")
        self.assertIn("case_not_closed", str(ctx.exception))

    def test_close_is_terminal_in_state_machine(self) -> None:
        case_id, _ = self._closed_case()
        with self.assertRaises(InvalidCaseState):
            self.svc.finalize_scope(self.legal, case_id)


class TimelineAndQueryTests(TerminationServiceTestBase):
    def test_timeline_is_append_only_and_ordered(self) -> None:
        case_id, _ = CloseAndLateMaterialTests._closed_case(self)
        self.svc.record_late_material(self.rd, case_id, "note", "REF", "迟到")
        timeline = self.svc.timeline(self.auditor, case_id)
        self.assertTrue(timeline["append_only"])
        types = [e["event_type"] for e in timeline["events"]]
        self.assertEqual(types[0], "case.notified")
        self.assertIn("scope.finalized", types)
        self.assertIn("case.closed", types)
        self.assertEqual(types[-1], "late_material.recorded")
        ids = [e["event_id"] for e in timeline["events"]]
        self.assertEqual(ids, sorted(ids))

    def test_rights_position_shows_disposable_rights_and_obligations(self) -> None:
        case_id, _ = CloseAndLateMaterialTests._closed_case(self)
        position = self.svc.rights_position(self.bd, case_id)
        self.assertTrue(position["closed"])
        subjects = {r["subject"] for r in position["current_disposable_rights"]}
        self.assertIn("CN 权利", subjects)
        self.assertIn("数据包", subjects)
        self.assertEqual([o["subject"] for o in position["legacy_obligations"]], ["患者随访"])
        self.assertEqual(position["unfinished_handovers"], [])

    def test_rights_position_before_close_marks_rights_not_disposable(self) -> None:
        case_id = self.open_case()
        self.finalize(case_id, [ScopeItemInput("right", "CN", "CN 权利", "revert", 0, "§3")])
        item = self.svc.case(self.legal, case_id)["items"][0]
        self.svc.start_handover(self.rd, case_id, item["item_id"])
        self.svc.complete_handover(self.rd, case_id, item["item_id"], "DEED")
        position = self.svc.rights_position(self.bd, case_id)
        self.assertFalse(position["closed"])
        self.assertEqual(position["current_disposable_rights"], [])
        self.assertEqual(len(position["pending_reversions"]), 1)

    def test_auditor_cannot_mutate(self) -> None:
        case_id = self.open_case()
        with self.assertRaises(PermissionError):
            self.svc.add_scope_item(self.auditor, case_id, right())


class PureDomainTests(unittest.TestCase):
    def test_window_math(self) -> None:
        self.assertEqual(add_days("2026-10-01T00:00:00+00:00", 30),
                         "2026-10-31T00:00:00+00:00")
        self.assertTrue(is_at_or_after("2026-10-31T00:00:00+00:00",
                                       "2026-10-31T00:00:00+00:00"))
        self.assertFalse(is_at_or_after("2026-10-30T23:59:59+00:00",
                                        "2026-10-31T00:00:00+00:00"))

    @staticmethod
    def _snapshot(status="handover_in_progress", items=(), dispute_open=False,
                  now="2026-12-01T00:00:00+00:00"):
        return CaseSnapshot(status=status, dispute_window_ends_at="2026-10-31T00:00:00+00:00",
                            has_open_dispute=dispute_open, items=tuple(items), now=now)

    def test_close_blockers_ignore_ongoing_obligations(self) -> None:
        snapshot = self._snapshot(items=[
            {"item_type": "right", "region": "CN", "subject": "r", "disposition": "revert",
             "status": "completed"},
            {"item_type": "obligation", "region": "global", "subject": "随访",
             "disposition": "ongoing", "status": "ongoing"},
        ])
        self.assertEqual(close_blockers(snapshot), [])

    def test_close_blockers_catch_incomplete_and_dispute(self) -> None:
        snapshot = self._snapshot(status="scope_finalized", dispute_open=True, items=[
            {"item_type": "data", "region": "global", "subject": "d", "disposition": "revert",
             "status": "in_handover"},
        ])
        blockers = close_blockers(snapshot)
        self.assertIn("open_dispute", blockers)
        self.assertTrue(any(b.startswith("handover_unconfirmed") for b in blockers))

    def test_clearance_blockers_require_close(self) -> None:
        blockers = clearance_blockers(self._snapshot(status="notified"), "relicense")
        self.assertIn("case_not_closed", blockers)


class HttpApiTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.service = TerminationService(":memory:",
                                         clock=lambda: "2026-10-02T09:00:00+00:00")
        bootstrap(cls.service)
        Handler.service = cls.service
        cls.server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        cls.port = cls.server.server_address[1]
        cls.thread = threading.Thread(target=cls.server.serve_forever, daemon=True)
        cls.thread.start()

    @classmethod
    def tearDownClass(cls) -> None:
        cls.server.shutdown()
        cls.server.server_close()
        cls.thread.join(timeout=2)

    def _post(self, path, payload, token=None):
        data = json.dumps(payload).encode()
        req = urllib.request.Request(
            f"http://127.0.0.1:{self.port}{path}", data=data, method="POST",
            headers={"Content-Type": "application/json",
                     **({"Authorization": f"Bearer {token}"} if token else {})})
        try:
            with urllib.request.urlopen(req) as resp:
                return resp.status, json.loads(resp.read())
        except urllib.error.HTTPError as e:
            return e.code, json.loads(e.read())

    def _get(self, path, token):
        req = urllib.request.Request(f"http://127.0.0.1:{self.port}{path}",
                                     headers={"Authorization": f"Bearer {token}"})
        with urllib.request.urlopen(req) as resp:
            return resp.status, json.loads(resp.read())

    def test_health_and_workflow(self) -> None:
        status, body = self._get("/health", "")
        self.assertEqual(status, 200)
        _, login = self._post("/login", {"user_id": "legal", "password": "legal-pass-2026"})
        token = login["token"]
        status, agreement = self._post("/agreements", {
            "agreement_id": "AGR-HTTP", "title": "HTTP 授权", "counterparty": "Acme",
            "signed_at": "2023-01-01T00:00:00+00:00"}, token)
        self.assertEqual(status, 201)
        status, opened = self._post("/termination_cases", {
            "agreement_id": "AGR-HTTP", "title": "终止", "trigger_type": "clinical_outcome",
            "reason": "II 期未达终点", "notice_key": "HTTP-N1",
            "notice_received_at": "2026-10-01T00:00:00+00:00",
            "dispute_window_days": 0}, token)
        self.assertEqual(status, 201)
        self.assertFalse(opened["duplicate"])
        case_id = opened["case"]["case_id"]
        status, item = self._post(f"/cases/{case_id}/scope_items", {
            "item_type": "right", "region": "CN", "subject": "CN 权利",
            "disposition": "revert", "source_amendment_seq": 0,
            "clause_ref": "Original §3"}, token)
        self.assertEqual(status, 201)
        # 争议窗口为 0 天，直接定稿
        status, finalized = self._post(f"/cases/{case_id}/finalize_scope", {}, token)
        self.assertEqual(status, 200)
        self.assertEqual(finalized["status"], "scope_finalized")
        status, position = self._get(f"/cases/{case_id}/rights_position", token)
        self.assertEqual(status, 200)
        self.assertEqual(position["case_status"], "scope_finalized")

    def test_duplicate_notice_over_http(self) -> None:
        _, login = self._post("/login", {"user_id": "legal", "password": "legal-pass-2026"})
        token = login["token"]
        self._post("/agreements", {
            "agreement_id": "AGR-DUP-HTTP", "title": "t", "counterparty": "Acme",
            "signed_at": "2023-01-01T00:00:00+00:00"}, token)
        payload = {"agreement_id": "AGR-DUP-HTTP", "title": "t",
                   "trigger_type": "mutual", "reason": "x", "notice_key": "DUP-1",
                   "notice_received_at": "2026-10-01T00:00:00+00:00",
                   "dispute_window_days": 5}
        _, first = self._post("/termination_cases", payload, token)
        _, second = self._post("/termination_cases", payload, token)
        self.assertFalse(first["duplicate"])
        self.assertTrue(second["duplicate"])


if __name__ == "__main__":
    unittest.main()
