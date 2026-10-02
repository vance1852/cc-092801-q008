from __future__ import annotations

import json
import sqlite3
import unittest
from datetime import datetime, timezone

from reversion_ops.api import JsonApplication
from reversion_ops.clock import FrozenClock
from reversion_ops.service import ReversionService


def sha(letter: str) -> str:
    return letter * 64


class ReversionApiTests(unittest.TestCase):
    def setUp(self) -> None:
        self.connection = sqlite3.connect(":memory:", isolation_level=None)
        self.connection.row_factory = sqlite3.Row
        self.clock = FrozenClock(datetime(2026, 10, 2, 8, 0, tzinfo=timezone.utc))
        self.service = ReversionService(self.connection, self.clock)
        self.app = JsonApplication(self.service)
        for uid, role in (("legal", "legal"), ("rd", "rd"), ("bd", "bd"),
                          ("receiver", "receiver"), ("auditor", "auditor")):
            self.service.create_user(uid, uid, role)

    def tearDown(self) -> None:
        self.connection.close()

    def call(self, method: str, path: str, actor: str | None = "legal", payload=None):
        body = json.dumps(payload).encode() if payload is not None else b""
        headers = {"X-Actor-Id": actor} if actor else {}
        return self.app.handle(method, path, headers, body)

    def _deal(self) -> None:
        self.call("POST", "/agreements", "legal", {
            "agreement_id": "agr", "revision_seq": 0, "title": "原协议",
            "kind": "original", "effective_date": "2023-05-01", "content_sha256": sha("a")})
        self.call("POST", "/agreements", "legal", {
            "agreement_id": "agr", "revision_seq": 1, "title": "修订一",
            "kind": "amendment", "effective_date": "2025-02-01",
            "supersedes_revision": 0, "content_sha256": sha("b")})
        self.call("POST", "/deals", "legal", {
            "deal_id": "deal", "name": "授权", "agreement_id": "agr"})
        self.call("POST", "/deals/deal/agreement_revisions", "legal", {"revision_seq": 1})
        self.call("POST", "/deals/deal/territories", "rd", {
            "territory_id": "t1", "region": "中国大陆", "rights_scope": "独家", "affected": True})
        self.call("POST", "/deals/deal/grants", "bd", {
            "grant_id": "g1", "territory_id": "t1", "counterparty": "分许可方",
            "grant_kind": "sublicense", "affected": True})

    def test_health(self) -> None:
        response = self.app.handle("GET", "/health")
        self.assertEqual(response.status, 200)
        self.assertEqual(response.body["status"], "ok")

    def test_actor_required_and_bad_json(self) -> None:
        missing = self.app.handle("POST", "/deals", body=b"{}")
        self.assertEqual(missing.status, 422)
        bad = self.app.handle("POST", "/deals", {"X-Actor-Id": "legal"}, b"not-json")
        self.assertEqual(bad.status, 422)
        self.assertEqual(bad.body["error"]["code"], "validation_failed")

    def test_full_flow_over_http(self) -> None:
        self._deal()
        notice = {"termination_id": "term", "deal_id": "deal", "notice_key": "n1",
                  "reason": "战略调整", "dispute_window_days": 30,
                  "effective_date": "2026-10-02", "partial_scope": True,
                  "scope_note": "仅中国大陆"}
        first = self.call("POST", "/terminations", "legal", notice)
        self.assertEqual(first.status, 201)
        duplicate = self.call("POST", "/terminations", "legal", notice)
        self.assertTrue(duplicate.body["duplicate"])

        self.call("POST", "/terminations/term/assets", "rd", {
            "asset_id": "ar", "asset_kind": "territory_right", "title": "区域权利",
            "source_revision_seq": 0, "source_clause": "2.1"})
        self.call("POST", "/terminations/term/assets", "rd", {
            "asset_id": "ad", "asset_kind": "data", "title": "数据",
            "source_revision_seq": 1, "source_clause": "4.2"})
        self.assertEqual(self.call("POST", "/assets/ar/confirm", "rd").status, 200)
        self.assertEqual(self.call("POST", "/assets/ad/confirm", "rd").status, 200)
        self.clock.advance(days=31)
        self.assertEqual(self.call("POST", "/terminations/term/winddown", "legal").status, 200)

        self.assertEqual(self.call("POST", "/terminations/term/handovers", "rd", {
            "batch_id": "b1", "asset_id": "ad",
            "manifest": {"f": ["x"]}, "content_sha256": sha("c")}).status, 201)
        received = self.call("POST", "/terminations/term/handovers/b1/receive", "receiver",
                             {"accepted": True, "note": "一致"})
        self.assertEqual(received.status, 200)
        self.assertEqual(received.body["asset_status"], "returned")

        self.call("POST", "/grants/g1/dispose", "bd", {"state": "terminated", "note": "终止"})
        self.call("POST", "/terminations/term/prerequisites", "rd",
                  {"prerequisite_id": "p1", "title": "结算", "required_for": "close"})
        self.call("POST", "/prerequisites/p1/resolve", "rd", {"satisfied": True, "note": "ok"})
        closed = self.call("POST", "/terminations/term/close", "legal",
                           {"decision": "closed_with_obligations"})
        self.assertEqual(closed.status, 200)
        self.assertEqual(closed.body["deal_state"], "closed")

        rights = self.call("GET", "/deals/deal/rights", "auditor")
        self.assertEqual([r["territory_id"] for r in rights.body["current_disposable_rights"]], ["t1"])
        chain = self.call("GET", "/timeline/verify", "auditor")
        self.assertTrue(chain.body["valid"])

    def test_forbidden_role(self) -> None:
        self._deal()
        response = self.call("POST", "/terminations", "rd", {
            "termination_id": "term", "deal_id": "deal", "notice_key": "n1",
            "reason": "r", "dispute_window_days": 1, "effective_date": "2026-10-02"})
        self.assertEqual(response.status, 403)
        self.assertEqual(response.body["error"]["code"], "forbidden")

    def test_route_not_found(self) -> None:
        response = self.call("GET", "/nope", "auditor")
        self.assertEqual(response.status, 404)


if __name__ == "__main__":
    unittest.main()
