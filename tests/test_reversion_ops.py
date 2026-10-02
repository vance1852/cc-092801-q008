from __future__ import annotations

import sqlite3
import unittest
from datetime import datetime, timezone

from reversion_ops.clock import FrozenClock
from reversion_ops.errors import Conflict, Forbidden, InvalidState, NotFound, ValidationFailed
from reversion_ops.service import ReversionService


def sha(letter: str) -> str:
    return letter * 64


class ReversionFixture(unittest.TestCase):
    def setUp(self) -> None:
        self.connection = sqlite3.connect(":memory:", isolation_level=None)
        self.connection.row_factory = sqlite3.Row
        self.clock = FrozenClock(datetime(2026, 10, 2, 8, 0, tzinfo=timezone.utc))
        self.service = ReversionService(self.connection, self.clock)
        for uid, name, role in (
            ("legal", "法务", "legal"),
            ("rd", "研发", "rd"),
            ("bd", "商务", "bd"),
            ("receiver", "接收方", "receiver"),
            ("auditor", "审计", "auditor"),
        ):
            self.service.create_user(uid, name, role)
        self.service.register_agreement_revision("legal", {
            "agreement_id": "agr", "revision_seq": 0, "title": "原协议",
            "kind": "original", "effective_date": "2023-05-01", "content_sha256": sha("a")})
        self.service.register_agreement_revision("legal", {
            "agreement_id": "agr", "revision_seq": 1, "title": "修订一",
            "kind": "amendment", "effective_date": "2025-02-01",
            "supersedes_revision": 0, "content_sha256": sha("b")})
        self.service.create_deal("legal", "deal", "授权交易", "agr")
        self.service.adopt_agreement_revision("legal", "deal", 1)
        self.service.add_territory("rd", "deal", {
            "territory_id": "t1", "region": "中国大陆", "rights_scope": "独家权利", "affected": True})
        self.service.add_territory("rd", "deal", {
            "territory_id": "t2", "region": "欧洲", "rights_scope": "独家权利", "affected": False})
        self.service.add_third_party_grant("bd", "deal", {
            "grant_id": "g1", "territory_id": "t1", "counterparty": "分许可方",
            "grant_kind": "sublicense", "affected": True})
        self.service.add_third_party_grant("bd", "deal", {
            "grant_id": "g2", "territory_id": "t2", "counterparty": "共同开发方",
            "grant_kind": "co_development", "affected": False})

    def tearDown(self) -> None:
        self.connection.close()

    def notify(self, *, window: int = 30, partial: bool = True) -> dict:
        return self.service.notify_termination("legal", {
            "termination_id": "term", "deal_id": "deal", "notice_key": "notice-1",
            "reason": "战略调整", "dispute_window_days": window,
            "effective_date": "2026-10-02", "partial_scope": partial,
            "scope_note": "仅中国大陆" if partial else "",
        })

    def list_assets_ready(self) -> None:
        """确认受影响资产并推进到收尾。"""
        self.service.add_asset_item("rd", "term", {
            "asset_id": "ar", "asset_kind": "territory_right", "title": "区域权利",
            "source_revision_seq": 0, "source_clause": "2.1", "affected": True})
        self.service.add_asset_item("rd", "term", {
            "asset_id": "ad", "asset_kind": "data", "title": "临床数据",
            "source_revision_seq": 1, "source_clause": "4.2", "affected": True})
        self.service.confirm_asset("rd", "ar")
        self.service.confirm_asset("rd", "ad")
        self.clock.advance(days=31)
        self.service.begin_wind_down("legal", "term")


class NoticeTests(ReversionFixture):
    def test_duplicate_notice_does_not_create_second_reversion(self) -> None:
        first = self.notify()
        second = self.notify()
        self.assertFalse(first["duplicate"])
        self.assertTrue(second["duplicate"])
        self.assertEqual(first["termination_id"], second["termination_id"])
        count = self.connection.execute("SELECT count(*) FROM terminations").fetchone()[0]
        self.assertEqual(count, 1)
        timeline = self.service.timeline_events("term")
        self.assertEqual([e["event_type"] for e in timeline].count("termination.noticed"), 1)

    def test_same_notice_key_different_content_conflicts(self) -> None:
        self.notify()
        with self.assertRaises(Conflict):
            self.service.notify_termination("legal", {
                "termination_id": "term", "deal_id": "deal", "notice_key": "notice-1",
                "reason": "不同理由", "dispute_window_days": 10,
                "effective_date": "2026-10-02", "partial_scope": False})

    def test_second_notice_while_one_open_rejected(self) -> None:
        self.notify()
        with self.assertRaises(InvalidState):
            self.service.notify_termination("legal", {
                "termination_id": "term2", "deal_id": "deal", "notice_key": "notice-2",
                "reason": "再次终止", "dispute_window_days": 30,
                "effective_date": "2026-10-02", "partial_scope": True})


class DisputeTests(ReversionFixture):
    def test_dispute_suspends_only_affected_grants(self) -> None:
        self.notify()
        self.service.raise_dispute("legal", "term", "数据归属异议")
        rights = self.service.deal_rights("auditor", "deal")
        grants = {g["grant_id"]: g["state"] for g in rights["grants"]}
        self.assertEqual(grants["g1"], "suspended")   # 受影响再许可暂停
        self.assertEqual(grants["g2"], "active")       # 共同开发继续执行

    def test_cannot_dispose_unaffected_grant(self) -> None:
        self.notify()
        self.service.raise_dispute("legal", "term", "异议")
        with self.assertRaises(InvalidState):
            self.service.dispose_grant("bd", "g2", "terminated", "尝试终止共同开发")

    def test_winddown_blocked_inside_dispute_window_and_with_open_dispute(self) -> None:
        self.notify()
        self.service.add_asset_item("rd", "term", {
            "asset_id": "ar", "asset_kind": "territory_right", "title": "权利",
            "source_revision_seq": 0, "source_clause": "2.1"})
        self.service.confirm_asset("rd", "ar")
        # 争议期未届满
        with self.assertRaises(InvalidState):
            self.service.begin_wind_down("legal", "term")
        self.clock.advance(days=31)
        # 无争议但需要先提出过——这里直接届满即可进入
        state = self.service.begin_wind_down("legal", "term")
        self.assertEqual(state["state"], "winding_down")

    def test_open_dispute_blocks_winddown_even_after_window(self) -> None:
        self.notify(window=5)
        self.service.raise_dispute("legal", "term", "异议")
        self.clock.advance(days=10)
        with self.assertRaises(InvalidState):
            self.service.begin_wind_down("legal", "term")
        self.service.resolve_dispute("legal", "term", "达成一致")
        self.service.add_asset_item("rd", "term", {
            "asset_id": "ar", "asset_kind": "territory_right", "title": "权利",
            "source_revision_seq": 0, "source_clause": "2.1"})
        self.service.confirm_asset("rd", "ar")
        state = self.service.begin_wind_down("legal", "term")
        self.assertEqual(state["state"], "winding_down")


class InventoryTests(ReversionFixture):
    def test_asset_must_cite_registered_adopted_revision(self) -> None:
        self.notify()
        # 未登记修订
        with self.assertRaises(ValidationFailed):
            self.service.add_asset_item("rd", "term", {
                "asset_id": "x", "asset_kind": "data", "title": "数据",
                "source_revision_seq": 9, "source_clause": "1"})
        # 必须带条款
        with self.assertRaises(ValidationFailed):
            self.service.add_asset_item("rd", "term", {
                "asset_id": "y", "asset_kind": "data", "title": "数据",
                "source_revision_seq": 1})

    def test_winddown_requires_confirmed_affected_assets(self) -> None:
        self.notify()
        self.service.add_asset_item("rd", "term", {
            "asset_id": "ad", "asset_kind": "data", "title": "数据",
            "source_revision_seq": 1, "source_clause": "4.2"})
        self.clock.advance(days=31)
        with self.assertRaises(InvalidState):  # 未确认
            self.service.begin_wind_down("legal", "term")

    def test_empty_affected_inventory_blocks_winddown(self) -> None:
        self.notify()
        self.clock.advance(days=31)
        with self.assertRaises(InvalidState):
            self.service.begin_wind_down("legal", "term")


class HandoverTests(ReversionFixture):
    def test_rejected_handover_blocks_close_and_resubmission_resolves(self) -> None:
        self.notify()
        self.list_assets_ready()
        self.service.submit_handover("rd", "term", {
            "batch_id": "b1", "asset_id": "ad",
            "manifest": {"f": ["x.csv"]}, "content_sha256": sha("c")})
        self.service.receive_handover("receiver", "term", "b1", False, "清单不符")
        self.service.add_prerequisite("rd", "term", "p1", "结算", "close")
        self.service.resolve_prerequisite("rd", "p1", True, "ok")
        with self.assertRaises(InvalidState):
            self.service.close_termination("legal", "term")
        self.assertEqual(
            self.service.close_readiness("auditor", "term")["state"], "preclose_blocked")
        # 重新提交并接收后可关闭
        self.service.submit_handover("rd", "term", {
            "batch_id": "b2", "asset_id": "ad",
            "manifest": {"f": ["x.csv", "y.csv"]}, "content_sha256": sha("e")})
        received = self.service.receive_handover("receiver", "term", "b2", True, "补齐")
        self.assertEqual(received["asset_status"], "returned")
        self.service.dispose_grant("bd", "g1", "terminated", "随回转终止")
        closed = self.service.close_termination("legal", "term")
        self.assertEqual(closed["state"], "closed")

    def test_handover_paused_during_dispute(self) -> None:
        self.notify()
        self.service.add_asset_item("rd", "term", {
            "asset_id": "ad", "asset_kind": "data", "title": "数据",
            "source_revision_seq": 1, "source_clause": "4.2"})
        self.service.confirm_asset("rd", "ad")
        self.service.raise_dispute("legal", "term", "争议")
        with self.assertRaises(InvalidState):
            self.service.submit_handover("rd", "term", {
                "batch_id": "b1", "asset_id": "ad",
                "manifest": {"f": ["x"]}, "content_sha256": sha("c")})


class CloseTests(ReversionFixture):
    def _ready_to_close(self) -> None:
        self.notify()
        self.list_assets_ready()
        self.service.submit_handover("rd", "term", {
            "batch_id": "b1", "asset_id": "ad",
            "manifest": {"f": ["x"]}, "content_sha256": sha("c")})
        self.service.receive_handover("receiver", "term", "b1", True, "ok")
        self.service.dispose_grant("bd", "g1", "terminated", "随回转终止")
        self.service.add_prerequisite("rd", "term", "p1", "结算", "close")
        self.service.resolve_prerequisite("rd", "p1", True, "ok")

    def test_close_is_terminal_and_late_material_does_not_reopen(self) -> None:
        self._ready_to_close()
        self.service.close_termination("legal", "term")
        with self.assertRaises(InvalidState):  # 决定不可重开
            self.service.close_termination("legal", "term")
        late = self.service.register_late_material(
            "receiver", "term", "迟到文件", sha("d"), asset_id="ad")
        self.assertFalse(late["reopened"])
        decision = self.connection.execute(
            "SELECT count(*) FROM close_decisions WHERE termination_id='term'").fetchone()[0]
        self.assertEqual(decision, 1)
        # 关闭后不能再增列资产或交付
        with self.assertRaises(InvalidState):
            self.service.add_asset_item("rd", "term", {
                "asset_id": "late", "asset_kind": "data", "title": "迟到",
                "source_revision_seq": 1, "source_clause": "9"})
        with self.assertRaises(InvalidState):
            self.service.submit_handover("rd", "term", {
                "batch_id": "b9", "asset_id": "ad",
                "manifest": {"f": ["z"]}, "content_sha256": sha("f")})

    def test_late_material_only_when_closed(self) -> None:
        self.notify()
        with self.assertRaises(InvalidState):
            self.service.register_late_material("receiver", "term", "文件", sha("d"))

    def test_partial_close_reverts_only_affected_territory_and_keeps_deal_active(self) -> None:
        self._ready_to_close()
        result = self.service.close_termination("legal", "term")
        self.assertEqual(result["deal_state"], "active")
        rights = self.service.deal_rights("auditor", "deal")
        self.assertEqual([r["territory_id"] for r in rights["current_disposable_rights"]], ["t1"])
        states = {t["territory_id"]: t["state"] for t in rights["territories"]}
        self.assertEqual(states["t1"], "reverted")
        self.assertEqual(states["t2"], "licensed")

    def test_full_close_closes_deal(self) -> None:
        self.notify(partial=False)
        self.service.add_asset_item("rd", "term", {
            "asset_id": "ar", "asset_kind": "territory_right", "title": "全部权利",
            "source_revision_seq": 0, "source_clause": "2.1"})
        self.service.confirm_asset("rd", "ar")
        self.clock.advance(days=31)
        self.service.begin_wind_down("legal", "term")
        self.service.dispose_grant("bd", "g1", "terminated", "终止")
        self.service.add_prerequisite("rd", "term", "p1", "结算", "close")
        self.service.resolve_prerequisite("rd", "p1", True, "ok")
        result = self.service.close_termination("legal", "term")
        self.assertEqual(result["deal_state"], "closed")


class ResumptionTests(ReversionFixture):
    def _closed(self) -> None:
        self.notify()
        self.list_assets_ready()
        self.service.submit_handover("rd", "term", {
            "batch_id": "b1", "asset_id": "ad",
            "manifest": {"f": ["x"]}, "content_sha256": sha("c")})
        self.service.receive_handover("receiver", "term", "b1", True, "ok")
        self.service.dispose_grant("bd", "g1", "terminated", "终止")
        self.service.add_prerequisite("rd", "term", "pc", "结算", "close")
        self.service.resolve_prerequisite("rd", "pc", True, "ok")
        self.service.close_termination("legal", "term")

    def test_resume_dev_requires_all_prerequisites_and_close(self) -> None:
        # 未关闭不放行
        self.notify()
        with self.assertRaises(InvalidState):
            self.service.grant_resumption("rd", "deal", "resume_dev", "t1")
        self.list_assets_ready()
        self.service.submit_handover("rd", "term", {
            "batch_id": "b1", "asset_id": "ad",
            "manifest": {"f": ["x"]}, "content_sha256": sha("c")})
        self.service.receive_handover("receiver", "term", "b1", True, "ok")
        self.service.dispose_grant("bd", "g1", "terminated", "终止")
        with self.assertRaises(InvalidState):  # 关闭前置未满足
            self.service.grant_resumption("rd", "deal", "resume_dev", "t1")
        self.service.add_prerequisite("rd", "term", "pc", "结算", "close")
        self.service.resolve_prerequisite("rd", "pc", True, "ok")
        self.service.close_termination("legal", "term")
        self.service.add_prerequisite("rd", "term", "pr", "数据核对", "resume_dev")
        with self.assertRaises(InvalidState):  # 自研前置未满足
            self.service.grant_resumption("rd", "deal", "resume_dev", "t1")
        self.service.resolve_prerequisite("rd", "pr", True, "ok")
        clearance = self.service.grant_resumption("rd", "deal", "resume_dev", "t1")
        self.assertFalse(clearance["duplicate"])
        # 再次放行幂等
        again = self.service.grant_resumption("rd", "deal", "resume_dev", "t1")
        self.assertTrue(again["duplicate"])

    def test_cannot_resume_non_reverted_territory(self) -> None:
        self._closed()
        with self.assertRaises(InvalidState):
            self.service.grant_resumption("bd", "deal", "relicense", "t2")

    def test_role_separation(self) -> None:
        self._closed()
        with self.assertRaises(Forbidden):
            self.service.grant_resumption("bd", "deal", "resume_dev", "t1")
        with self.assertRaises(Forbidden):
            self.service.grant_resumption("rd", "deal", "relicense", "t1")
        with self.assertRaises(Forbidden):
            self.service.notify_termination("rd", {
                "termination_id": "z", "deal_id": "deal", "notice_key": "nz",
                "reason": "r", "dispute_window_days": 1, "effective_date": "2026-10-02"})


class TimelineTests(ReversionFixture):
    def test_timeline_is_append_only_hash_chain(self) -> None:
        self.notify()
        before = self.service.verify_timeline("auditor", "term")
        self.assertTrue(before["valid"])
        # 直接篡改底层事件即破坏哈希链
        self.connection.execute(
            "UPDATE termination_timeline_events SET payload_json=? WHERE event_id=1",
            ('{"tampered":true}',))
        after = self.service.verify_timeline("auditor", "term")
        self.assertFalse(after["valid"])

    def test_timeline_per_termination_independent(self) -> None:
        self.notify()
        self.list_assets_ready()
        events = self.service.timeline_events("term")
        self.assertEqual(events[0]["previous_hash"], "0" * 64)
        for earlier, later in zip(events, events[1:]):
            self.assertEqual(later["previous_hash"], earlier["event_hash"])

    def test_global_verification_valid_with_two_independent_chains(self) -> None:
        # 第一条终止产生若干事件
        self.notify()
        self.list_assets_ready()
        # 第二条独立交易的终止，形成另一条独立哈希链
        self.service.create_deal("legal", "deal2", "第二笔授权", "agr")
        self.service.add_territory("rd", "deal2", {
            "territory_id": "z1", "region": "日本", "rights_scope": "独家", "affected": True})
        self.service.notify_termination("legal", {
            "termination_id": "term2", "deal_id": "deal2", "notice_key": "notice-2",
            "reason": "临床结果变化", "dispute_window_days": 0,
            "effective_date": "2026-10-02", "partial_scope": False})
        global_chain = self.service.verify_timeline("auditor")
        self.assertTrue(global_chain["valid"])
        self.assertEqual(global_chain["chains"], 2)
        self.assertIsNone(global_chain["head_hash"])


class QueryTests(ReversionFixture):
    def test_rights_report_shows_disposable_obligations_handover(self) -> None:
        self.notify()
        self.service.add_asset_item("rd", "term", {
            "asset_id": "ar", "asset_kind": "territory_right", "title": "区域权利",
            "source_revision_seq": 0, "source_clause": "2.1"})
        self.service.add_asset_item("rd", "term", {
            "asset_id": "as", "asset_kind": "sample", "title": "样本",
            "source_revision_seq": 1, "source_clause": "4.3"})
        self.service.confirm_asset("rd", "ar")
        self.service.confirm_asset("rd", "as")
        self.service.dispose_asset("rd", "as", "held_over", "样本托管延续")
        self.clock.advance(days=31)
        self.service.begin_wind_down("legal", "term")
        self.service.dispose_grant("bd", "g1", "surviving", "供货义务延续")
        self.service.add_prerequisite("rd", "term", "p1", "结算", "close")
        report = self.service.deal_rights("auditor", "deal")
        # 关闭前无回转权利；有遗留义务与未完成前置
        self.assertEqual(report["current_disposable_rights"], [])
        kinds = {o.get("asset_kind") or o.get("grant_kind") for o in report["residual_obligations"]}
        self.assertIn("sample", kinds)
        self.assertIn("sublicense", kinds)
        pending = {p["prerequisite_id"] for p in report["incomplete_handover"]}
        self.assertIn("p1", pending)

    def test_missing_entities_raise_not_found(self) -> None:
        with self.assertRaises(NotFound):
            self.service.termination_overview("auditor", "nope")
        with self.assertRaises(NotFound):
            self.service.deal_rights("auditor", "nope")

    def test_auditor_cannot_mutate(self) -> None:
        with self.assertRaises(Forbidden):
            self.service.create_deal("auditor", "d9", "x", "agr")


if __name__ == "__main__":
    unittest.main()
