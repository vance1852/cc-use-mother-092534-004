"""提供不依赖第三方框架的 HTTP/JSON 边界。"""

from __future__ import annotations

import argparse
import json
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Any
from urllib.parse import parse_qs, urlparse

from .errors import DomainError, ValidationError
from .festival import FestivalService
from .service import DomainService
from .storage import Database


def _query_value(parsed, name: str) -> str:
    value = parse_qs(parsed.query).get(name, [""])[0]
    if not value:
        raise ValidationError(f"{name} 不能为空")
    return value


def _festival_route(festival: FestivalService, method: str, parsed,
                    body: dict[str, Any], actor_id: str) -> tuple[int, dict[str, Any]]:
    """分派中秋活动编排领域的接口。"""

    path = parsed.path

    def created(result: dict[str, Any]) -> tuple[int, dict[str, Any]]:
        return (200 if result.get("replayed") else 201), result

    if method == "POST" and path == "/festival/units":
        return created(festival.submit_unit(actor_id=actor_id, **body))
    if method == "GET" and path == "/festival/units":
        site_id = _query_value(parsed, "site_id")
        return 200, {"items": [unit.__dict__ for unit in festival.list_units(site_id)]}
    if method == "GET" and path == "/festival/unit":
        return 200, festival.get_unit(_query_value(parsed, "unit_id")).__dict__
    if method == "POST" and path == "/festival/resources":
        return created(festival.update_resource(actor_id=actor_id, **body))
    if method == "GET" and path == "/festival/resources":
        site_id = _query_value(parsed, "site_id")
        resource_type = parse_qs(parsed.query).get("resource_type", [None])[0]
        return 200, {"items": [item.__dict__ for item in festival.list_resources(site_id, resource_type)]}
    if method == "POST" and path == "/festival/plans":
        return created(festival.generate_plan(actor_id=actor_id, **body))
    if method == "GET" and path == "/festival/plans":
        return 200, {"items": festival.list_plans(_query_value(parsed, "site_id"))}
    if method == "GET" and path == "/festival/plan":
        return 200, festival.get_plan(_query_value(parsed, "plan_id"))
    if method == "POST" and path == "/festival/plans/sign-off":
        return created(festival.sign_off(actor_id=actor_id, **body))
    if method == "POST" and path == "/festival/plans/release":
        return created(festival.release_plan(actor_id=actor_id, **body))
    if method == "POST" and path == "/festival/units/transition":
        return created(festival.transition_unit(actor_id=actor_id, **body))
    if method == "POST" and path == "/festival/impacts":
        return created(festival.report_impact(actor_id=actor_id, **body))
    if method == "GET" and path == "/festival/impacts":
        return 200, {"items": festival.list_impacts(_query_value(parsed, "site_id"))}
    if method == "GET" and path == "/festival/impact":
        return 200, festival.get_impact(_query_value(parsed, "impact_id"))
    if method == "GET" and path == "/festival/clearance":
        return 200, festival.clearance(_query_value(parsed, "site_id"))
    if method == "GET" and path == "/festival/capacity":
        return 200, festival.capacity_view(_query_value(parsed, "site_id"))
    if method == "GET" and path == "/festival/responsibility":
        return 200, festival.responsibility_chain(_query_value(parsed, "plan_id"))
    return 404, {"error": "route_not_found", "message": "接口不存在"}


def route(service: DomainService, method: str, path: str, body: dict[str, Any] | None,
          headers: dict[str, str] | None = None,
          festival: FestivalService | None = None) -> tuple[int, dict[str, Any]]:
    """把一个 HTTP 语义请求分派到领域服务。"""

    headers = headers or {}
    body = body or {}
    parsed = urlparse(path)
    actor_id = headers.get("X-Actor-Id", "")
    try:
        if parsed.path.startswith("/festival/"):
            if festival is None:
                return 404, {"error": "route_not_found", "message": "接口不存在"}
            return _festival_route(festival, method, parsed, body, actor_id)
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
            query = parse_qs(parsed.query)
            site_id = query.get("site_id", [""])[0]
            if not site_id:
                raise ValidationError("site_id 不能为空")
            category = query.get("category", [None])[0]
            return 200, {"items": [item.__dict__ for item in service.list_domain_data(site_id, category)]}
        if method == "GET" and parsed.path == "/audit-events":
            query = parse_qs(parsed.query)
            after = int(query.get("after_sequence", ["0"])[0])
            return 200, {"items": service.audit_events(after)}
        return 404, {"error": "route_not_found", "message": "接口不存在"}
    except DomainError as exc:
        return exc.status, {"error": exc.code, "message": str(exc)}
    except (TypeError, ValueError) as exc:
        return 400, {"error": "invalid_request", "message": str(exc)}


class Handler(BaseHTTPRequestHandler):
    """把标准库 HTTP 请求转换为路由调用。"""

    service: DomainService
    festival: FestivalService

    def _handle(self) -> None:
        length = int(self.headers.get("Content-Length", "0"))
        raw = self.rfile.read(length) if length else b"{}"
        try:
            body = json.loads(raw.decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError):
            self._write(400, {"error": "invalid_json", "message": "请求体必须是 UTF-8 JSON"})
            return
        status, payload = route(self.service, self.command, self.path, body,
                                {"X-Actor-Id": self.headers.get("X-Actor-Id", "")},
                                festival=getattr(self, "festival", None))
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

    parser = argparse.ArgumentParser(description="启动节日公共服务协作基础层")
    parser.add_argument("--database", default="service.sqlite3")
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=8080)
    args = parser.parse_args()
    database = Database(args.database)
    Handler.service = DomainService(database)
    Handler.festival = FestivalService(Handler.service)
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
