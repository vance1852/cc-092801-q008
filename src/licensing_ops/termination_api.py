"""交易终止与权利回转的 JSON HTTP API（仅依赖标准库）。"""

from __future__ import annotations

import argparse
import json
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

from .termination import ScopeItemInput
from .termination_service import TerminationService


class Handler(BaseHTTPRequestHandler):
    service = TerminationService()

    def _send(self, status, payload):
        data = json.dumps(payload, ensure_ascii=False).encode()
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    def _token(self):
        return self.headers.get("Authorization", "").removeprefix("Bearer ")

    def _body(self):
        return json.loads(self.rfile.read(int(self.headers.get("Content-Length", "0"))) or b"{}")

    def _segments(self):
        return [p for p in self.path.strip("/").split("/") if p]

    def log_message(self, fmt, *args):  # 静默访问日志
        return

    # ------------------------------------------------------------------

    def do_GET(self):
        try:
            if self.path == "/health":
                return self._send(200, {"status": "ok", "service": "deal-termination-reversion"})
            parts = self._segments()
            token = self._token()
            if len(parts) == 2 and parts[0] == "agreements":
                return self._send(200, self.service.agreement(token, parts[1]))
            if len(parts) == 2 and parts[0] == "cases":
                return self._send(200, self.service.case(token, parts[1]))
            if len(parts) == 3 and parts[0] == "cases" and parts[2] == "timeline":
                return self._send(200, self.service.timeline(token, parts[1]))
            if len(parts) == 3 and parts[0] == "cases" and parts[2] == "rights_position":
                return self._send(200, self.service.rights_position(token, parts[1]))
            return self._send(404, {"error": "not found"})
        except KeyError as e:
            return self._send(404, {"error": f"not found: {e.args[0]}"})
        except PermissionError as e:
            return self._send(403, {"error": str(e)})
        except Exception as e:
            return self._send(400, {"error": str(e)})

    def do_POST(self):
        try:
            body = self._body()
            parts = self._segments()
            if self.path == "/login":
                return self._send(200, {"token": self.service.auth.login(body["user_id"], body["password"])})
            token = self._token()
            s = self.service

            if self.path == "/agreements":
                return self._send(201, s.register_agreement(
                    token, body["agreement_id"], body["title"], body["counterparty"], body["signed_at"]))
            if len(parts) == 3 and parts[0] == "agreements" and parts[2] == "amendments":
                return self._send(201, s.add_amendment(
                    token, parts[1], int(body["amendment_seq"]), body["title"],
                    body["signed_at"], body.get("summary", "")))

            if self.path == "/termination_cases":
                return self._send(201, s.open_termination_case(
                    token, body["agreement_id"], body["title"], body["trigger_type"], body["reason"],
                    body["notice_key"], body["notice_received_at"], int(body["dispute_window_days"]),
                    body.get("effective_termination_at")))
            if len(parts) == 3 and parts[0] == "cases" and parts[2] == "disputes":
                return self._send(201, s.raise_dispute(token, parts[1], body["topic"], body["detail"]))
            if len(parts) == 3 and parts[0] == "disputes" and parts[2] == "resolve":
                return self._send(200, s.resolve_dispute(token, parts[1], body["resolution_note"]))
            if len(parts) == 3 and parts[0] == "cases" and parts[2] == "scope_items":
                item = ScopeItemInput(
                    body["item_type"], body["region"], body["subject"], body["disposition"],
                    int(body["source_amendment_seq"]), body["clause_ref"],
                    bool(body.get("required", True)), body.get("due_at"), body.get("detail", {}))
                return self._send(201, s.add_scope_item(token, parts[1], item))
            if len(parts) == 3 and parts[0] == "cases" and parts[2] == "finalize_scope":
                return self._send(200, s.finalize_scope(token, parts[1]))
            if len(parts) == 4 and parts[0] == "cases" and parts[2] == "items" and parts[3] == "start_handover":
                return self._send(200, s.start_handover(token, parts[1], body["item_id"], body.get("note", "")))
            if len(parts) == 4 and parts[0] == "cases" and parts[2] == "items" and parts[3] == "complete_handover":
                return self._send(200, s.complete_handover(
                    token, parts[1], body["item_id"], body["evidence_ref"], body.get("note", "")))
            if len(parts) == 4 and parts[0] == "cases" and parts[2] == "items" and parts[3] == "suspend_part":
                return self._send(200, s.suspend_affected_part(
                    token, parts[1], body["item_id"], body["scope_note"], body.get("evidence_ref", "")))
            if len(parts) == 4 and parts[0] == "cases" and parts[2] == "items" and parts[3] == "resolve_collaboration":
                return self._send(200, s.resolve_collaboration(
                    token, parts[1], body["item_id"], body["outcome"],
                    body["evidence_ref"], body.get("note", "")))
            if len(parts) == 4 and parts[0] == "cases" and parts[2] == "items" and parts[3] == "resolve_sublicense":
                return self._send(200, s.resolve_sublicense(
                    token, parts[1], body["item_id"], body["action"],
                    body["evidence_ref"], body.get("note", "")))
            if len(parts) == 3 and parts[0] == "cases" and parts[2] == "close":
                return self._send(200, s.close_case(token, parts[1], body["close_decision"]))
            if len(parts) == 3 and parts[0] == "cases" and parts[2] == "late_materials":
                return self._send(201, s.record_late_material(
                    token, parts[1], body["material_type"], body["source_ref"], body.get("note", ""),
                    body.get("received_at"), body.get("item_id")))
            if len(parts) == 3 and parts[0] == "cases" and parts[2] == "clearance":
                return self._send(200, s.request_clearance(token, parts[1], body["kind"], body.get("region")))
            return self._send(404, {"error": "not found"})
        except KeyError as e:
            return self._send(404, {"error": f"not found: {e.args[0]}"})
        except PermissionError as e:
            return self._send(403, {"error": str(e)})
        except Exception as e:
            return self._send(400, {"error": str(e)})


def bootstrap(service: TerminationService) -> None:
    for uid, pwd, role in (
        ("legal", "legal-pass-2026", "legal"),
        ("rd", "rd-pass-2026", "rd"),
        ("bd", "bd-pass-2026", "bd"),
        ("auditor", "auditor-2026", "auditor"),
    ):
        try:
            service.auth.create_user(uid, pwd, role)
        except Exception:
            pass


def main():
    p = argparse.ArgumentParser()
    p.add_argument("--database", default=":memory:")
    p.add_argument("--host", default="127.0.0.1")
    p.add_argument("--port", type=int, default=8083)
    a = p.parse_args()
    Handler.service = TerminationService(a.database)
    bootstrap(Handler.service)
    ThreadingHTTPServer((a.host, a.port), Handler).serve_forever()


if __name__ == "__main__":
    main()
