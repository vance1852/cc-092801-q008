"""无第三方依赖的交易终止与权利回转 HTTP JSON 接口。"""

from __future__ import annotations

import argparse
import json
from dataclasses import dataclass
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any, Mapping
from urllib.parse import urlparse

from .errors import ReversionError, ValidationFailed
from .service import ReversionService
from .storage import connect


@dataclass(frozen=True, slots=True)
class Response:
    status: int
    body: Mapping[str, Any]


class JsonApplication:
    def __init__(self, service: ReversionService) -> None:
        self.service = service

    @staticmethod
    def _actor(headers: Mapping[str, str]) -> str:
        actor = headers.get("x-actor-id", "").strip()
        if not actor:
            raise ValidationFailed("缺少 X-Actor-Id")
        return actor

    @staticmethod
    def _json(body: bytes) -> dict[str, Any]:
        if not body:
            return {}
        try:
            value = json.loads(body.decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError) as exc:
            raise ValidationFailed("请求体必须是 UTF-8 JSON 对象") from exc
        if not isinstance(value, dict):
            raise ValidationFailed("请求体必须是 JSON 对象")
        return value

    def handle(self, method: str, target: str, headers: Mapping[str, str] | None = None,
               body: bytes = b"") -> Response:
        normalized = {key.lower(): value for key, value in (headers or {}).items()}
        path = urlparse(target).path.rstrip("/") or "/"
        parts = [part for part in path.split("/") if part]
        try:
            if method == "GET" and path == "/health":
                return Response(200, {"status": "ok"})
            payload = self._json(body) if method in {"POST", "PUT", "PATCH"} else {}
            actor = self._actor(normalized)

            if method == "POST" and path == "/users":
                return Response(201, self.service.create_user(
                    payload["user_id"], payload["display_name"], payload["role"]))

            if method == "POST" and path == "/agreements":
                return Response(201, self.service.register_agreement_revision(actor, payload))
            if method == "POST" and path == "/deals":
                return Response(201, self.service.create_deal(
                    actor, payload["deal_id"], payload["name"], payload["agreement_id"]))
            if method == "POST" and len(parts) == 3 and parts[0] == "deals" and parts[2] == "agreement_revisions":
                return Response(200, self.service.adopt_agreement_revision(
                    actor, parts[1], int(payload["revision_seq"])))
            if method == "POST" and len(parts) == 3 and parts[0] == "deals" and parts[2] == "territories":
                return Response(201, self.service.add_territory(actor, parts[1], payload))
            if method == "POST" and len(parts) == 3 and parts[0] == "deals" and parts[2] == "grants":
                return Response(201, self.service.add_third_party_grant(actor, parts[1], payload))

            if method == "POST" and path == "/terminations":
                return Response(201, self.service.notify_termination(actor, payload))
            if method == "POST" and len(parts) == 3 and parts[0] == "terminations" and parts[2] == "disputes":
                return Response(201, self.service.raise_dispute(actor, parts[1], payload["summary"]))
            if method == "POST" and len(parts) == 3 and parts[0] == "terminations" and parts[2] == "dispute_resolution":
                return Response(200, self.service.resolve_dispute(actor, parts[1], payload["note"]))
            if method == "POST" and len(parts) == 3 and parts[0] == "terminations" and parts[2] == "winddown":
                return Response(200, self.service.begin_wind_down(actor, parts[1]))
            if method == "POST" and len(parts) == 3 and parts[0] == "terminations" and parts[2] == "assets":
                return Response(201, self.service.add_asset_item(actor, parts[1], payload))
            if method == "POST" and len(parts) == 3 and parts[0] == "assets" and parts[2] == "confirm":
                return Response(200, self.service.confirm_asset(actor, parts[1]))
            if method == "POST" and len(parts) == 3 and parts[0] == "assets" and parts[2] == "dispose":
                return Response(200, self.service.dispose_asset(
                    actor, parts[1], payload["status"], payload["note"]))
            if method == "POST" and len(parts) == 3 and parts[0] == "terminations" and parts[2] == "handovers":
                return Response(201, self.service.submit_handover(actor, parts[1], payload))
            if method == "POST" and len(parts) == 5 and parts[0] == "terminations" \
                    and parts[2] == "handovers" and parts[4] == "receive":
                return Response(200, self.service.receive_handover(
                    actor, parts[1], parts[3], bool(payload["accepted"]), payload.get("note", "")))
            if method == "POST" and len(parts) == 3 and parts[0] == "grants" and parts[2] == "dispose":
                return Response(200, self.service.dispose_grant(
                    actor, parts[1], payload["state"], payload["note"]))
            if method == "POST" and len(parts) == 3 and parts[0] == "terminations" and parts[2] == "prerequisites":
                return Response(201, self.service.add_prerequisite(
                    actor, parts[1], payload["prerequisite_id"], payload["title"], payload["required_for"]))
            if method == "POST" and len(parts) == 3 and parts[0] == "prerequisites" and parts[2] == "resolve":
                return Response(200, self.service.resolve_prerequisite(
                    actor, parts[1], bool(payload["satisfied"]), payload.get("note", "")))
            if method == "GET" and len(parts) == 3 and parts[0] == "terminations" and parts[2] == "readiness":
                return Response(200, self.service.close_readiness(actor, parts[1]))
            if method == "POST" and len(parts) == 3 and parts[0] == "terminations" and parts[2] == "close":
                return Response(200, self.service.close_termination(
                    actor, parts[1], payload.get("decision", "closed")))
            if method == "POST" and len(parts) == 3 and parts[0] == "terminations" and parts[2] == "late_materials":
                return Response(201, self.service.register_late_material(
                    actor, parts[1], payload["title"], payload["content_sha256"],
                    payload.get("asset_id")))
            if method == "POST" and len(parts) == 3 and parts[0] == "deals" and parts[2] == "resumptions":
                return Response(201, self.service.grant_resumption(
                    actor, parts[1], payload["clearance_kind"],
                    payload.get("territory_id"), payload.get("note", "")))

            if method == "GET" and len(parts) == 3 and parts[0] == "deals" and parts[2] == "rights":
                return Response(200, self.service.deal_rights(actor, parts[1]))
            if method == "GET" and len(parts) == 2 and parts[0] == "terminations":
                return Response(200, self.service.termination_overview(actor, parts[1]))
            if method == "GET" and path == "/timeline/verify":
                return Response(200, self.service.verify_timeline(actor))

            return Response(404, {"error": {"code": "route_not_found", "message": "接口不存在"}})
        except ReversionError as exc:
            return Response(exc.status, {"error": {"code": exc.code, "message": str(exc)}})
        except (KeyError, TypeError, ValueError) as exc:
            return Response(422, {"error": {"code": "invalid_request", "message": str(exc)}})


def make_handler(application: JsonApplication):
    class Handler(BaseHTTPRequestHandler):
        server_version = "ReversionOps/1"

        def do_GET(self) -> None:  # noqa: N802
            self._dispatch()

        def do_POST(self) -> None:  # noqa: N802
            self._dispatch()

        def _dispatch(self) -> None:
            length = int(self.headers.get("Content-Length", "0"))
            body = self.rfile.read(length) if length else b""
            response = application.handle(self.command, self.path, dict(self.headers.items()), body)
            encoded = json.dumps(response.body, ensure_ascii=False, separators=(",", ":")).encode("utf-8")
            self.send_response(response.status)
            self.send_header("Content-Type", "application/json; charset=utf-8")
            self.send_header("Content-Length", str(len(encoded)))
            self.end_headers()
            self.wfile.write(encoded)

        def log_message(self, format: str, *args: object) -> None:
            return

    return Handler


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="启动交易终止与权利回转服务")
    parser.add_argument("--database", type=Path, default=Path("reversion_ops.sqlite3"))
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=8083)
    args = parser.parse_args(argv)
    connection = connect(args.database)
    server = ThreadingHTTPServer((args.host, args.port),
                                 make_handler(JsonApplication(ReversionService(connection))))
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        server.server_close()
        connection.close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
