"""提供不依赖第三方框架的 HTTP/JSON 边界。"""

from __future__ import annotations

import argparse
import json
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Any
from urllib.parse import parse_qs, urlparse

from .errors import DomainError, ValidationError
from .orchestration import OrchestrationService
from .service import DomainService
from .storage import Database


def route(service: DomainService, method: str, path: str, body: dict[str, Any] | None,
          headers: dict[str, str] | None = None) -> tuple[int, dict[str, Any]]:
    """把一个 HTTP 语义请求分派到领域服务。"""

    headers = headers or {}
    body = body or {}
    parsed = urlparse(path)
    actor_id = headers.get("X-Actor-Id", "")
    query = parse_qs(parsed.query)

    def q(name: str, default: str = "") -> str:
        return query.get(name, [default])[0]

    try:
        if method == "GET" and parsed.path == "/health":
            valid, count = service.verify_audit()
            return 200, {"status": "ok", "audit_valid": valid, "audit_events": count}
        if method == "POST" and parsed.path == "/organizations":
            receipt = service.register_organization(actor_id=actor_id, **body)
            return 200 if receipt.replayed else 201, receipt.__dict__
        if method == "POST" and parsed.path == "/actors":
            receipt = service.register_actor(actor_id=actor_id, **body)
            return 200 if receipt.replayed else 201, receipt.__dict__
        if method == "POST" and parsed.path == "/sites":
            receipt = service.register_site(actor_id=actor_id, **body)
            return 200 if receipt.replayed else 201, receipt.__dict__
        if method == "POST" and parsed.path == "/domain-records":
            receipt = service.record_domain_data(actor_id=actor_id, **body)
            return 200 if receipt.replayed else 201, receipt.__dict__
        if method == "GET" and parsed.path == "/domain-records":
            site_id = q("site_id")
            if not site_id:
                raise ValidationError("site_id 不能为空")
            category = query.get("category", [None])[0]
            return 200, {"items": [item.__dict__ for item in service.list_domain_data(site_id, category)]}
        if method == "GET" and parsed.path == "/audit-events":
            after = int(q("after_sequence", "0"))
            return 200, {"items": service.audit_events(after)}

        # ------------------------------------------------ 编排与安全放行中枢
        if not isinstance(service, OrchestrationService):
            return 404, {"error": "route_not_found", "message": "接口不存在"}
        result: dict[str, Any]
        if method == "POST" and parsed.path == "/site-resources":
            result = service.configure_site_resource(actor_id=actor_id, **body)
            return 200, result
        if method == "POST" and parsed.path == "/site-windows":
            result = service.upsert_site_window(actor_id=actor_id, **body)
            return 200, result
        if method == "POST" and parsed.path == "/certifications":
            result = service.grant_certification(actor_id=actor_id, **body)
            return 200, result
        if method == "POST" and parsed.path == "/safety-checks":
            result = service.assign_safety_check(actor_id=actor_id, **body)
            return 200, result
        if method == "POST" and parsed.path == "/plans":
            result = service.submit_plan(actor_id=actor_id, **body)
            return 200, result
        if method == "POST" and parsed.path == "/plans/discard":
            result = service.discard_draft(actor_id=actor_id, **body)
            return 200, result
        if method == "POST" and parsed.path == "/plans/sign":
            result = service.sign_check(actor_id=actor_id, **body)
            return 200, result
        if method == "POST" and parsed.path == "/plans/publish":
            result = service.publish_plan(actor_id=actor_id, **body)
            return 200, result
        if method == "GET" and parsed.path == "/plans/gate":
            plan_id = q("plan_id")
            if not plan_id:
                raise ValidationError("plan_id 不能为空")
            return 200, service.get_plan_gate(plan_id)
        if method == "GET" and parsed.path == "/release-board":
            return 200, service.release_board(q("site_id") or None)
        if method == "GET" and parsed.path == "/capacity-ledger":
            site_id = q("site_id")
            if not site_id:
                raise ValidationError("site_id 不能为空")
            return 200, service.capacity_ledger(site_id)
        if method == "POST" and parsed.path == "/incidents/rain":
            result = service.declare_rain_alert(actor_id=actor_id, **body)
            return 200, result
        if method == "POST" and parsed.path == "/incidents/facility":
            result = service.report_facility_change(actor_id=actor_id, **body)
            return 200, result
        if method == "GET" and parsed.path == "/impact-analysis":
            return 200, service.analyze_impact(q("plan_id") or None)
        if method == "POST" and parsed.path == "/relocations/accept":
            result = service.accept_relocation(actor_id=actor_id, **body)
            return 200, result
        unit_actions = {
            "/units/start": "start_unit",
            "/units/pause": "pause_unit",
            "/units/resume": "resume_unit",
            "/units/end": "end_unit",
            "/units/cancel": "cancel_unit",
        }
        if method == "POST" and parsed.path in unit_actions:
            result = getattr(service, unit_actions[parsed.path])(actor_id=actor_id, **body)
            return 200, result
        if method == "POST" and parsed.path == "/units/manual-resolve":
            result = service.resolve_manual_unit(actor_id=actor_id, **body)
            return 200, result
        return 404, {"error": "route_not_found", "message": "接口不存在"}
    except DomainError as exc:
        return exc.status, {"error": exc.code, "message": str(exc)}
    except (TypeError, ValueError) as exc:
        return 400, {"error": "invalid_request", "message": str(exc)}


class Handler(BaseHTTPRequestHandler):
    """把标准库 HTTP 请求转换为路由调用。"""

    service: DomainService

    def _handle(self) -> None:
        length = int(self.headers.get("Content-Length", "0"))
        raw = self.rfile.read(length) if length else b"{}"
        try:
            body = json.loads(raw.decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError):
            self._write(400, {"error": "invalid_json", "message": "请求体必须是 UTF-8 JSON"})
            return
        status, payload = route(self.service, self.command, self.path, body,
                                {"X-Actor-Id": self.headers.get("X-Actor-Id", "")})
        self._write(status, payload)

    def _write(self, status: int, payload: dict[str, Any]) -> None:
        data = json.dumps(payload, ensure_ascii=False, separators=(",", ":")).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    def do_GET(self) -> None:
        self._handle()

    def do_POST(self) -> None:
        self._handle()

    def log_message(self, format: str, *args: object) -> None:
        return


def main() -> int:
    """启动本地 HTTP 服务。"""

    parser = argparse.ArgumentParser(description="启动中秋活动承载与安全放行中枢")
    parser.add_argument("--database", default="service.sqlite3")
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=8080)
    args = parser.parse_args()
    database = Database(args.database)
    Handler.service = OrchestrationService(database)
    server = ThreadingHTTPServer((args.host, args.port), Handler)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        server.server_close()
        database.close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
