"""HTTP JSON API（标准库实现，无第三方依赖）。

鉴权通过请求头传递（演示用）：

- ``X-Actor-Role``   dispatcher / venue_staff / agency / visitor_service
- ``X-Actor-Name``   操作人姓名（写入时间线）
- ``X-Agency-Id``    旅行社主体必填
- ``X-Venue-Id``     景区工作人员必填

核心写操作都由 :class:`Service` 在串行化事务中完成，因此多线程
并发请求下容量仍然安全。
"""

from __future__ import annotations

import json
import re
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import urlparse

from .errors import DomainError, PermissionDeniedError
from .security import Actor
from .service import Service


def _json_response(handler: BaseHTTPRequestHandler, status: int, body) -> None:
    data = json.dumps(body, ensure_ascii=False, indent=2).encode("utf-8")
    handler.send_response(status)
    handler.send_header("Content-Type", "application/json; charset=utf-8")
    handler.send_header("Content-Length", str(len(data)))
    handler.end_headers()
    handler.wfile.write(data)


class App:
    def __init__(self, service: Service) -> None:
        self.service = service

    def make_handler(self):
        app = self

        class Handler(BaseHTTPRequestHandler):
            server_version = "TourismCapacity/0.1"

            def log_message(self, fmt, *args):  # 静默，测试输出保持干净
                return

            # -- 基础工具 ------------------------------------------------ #

            def _actor(self) -> Actor:
                role = self.headers.get("X-Actor-Role", "").strip()
                name = self.headers.get("X-Actor-Name", "anonymous").strip()
                if not role:
                    raise PermissionDeniedError("缺少 X-Actor-Role 请求头")
                return Actor(
                    role=role,
                    name=name,
                    agency_id=self.headers.get("X-Agency-Id") or None,
                    venue_id=self.headers.get("X-Venue-Id") or None,
                )

            def _body(self) -> dict:
                length = int(self.headers.get("Content-Length") or 0)
                if length == 0:
                    return {}
                raw = self.rfile.read(length)
                try:
                    value = json.loads(raw.decode("utf-8"))
                except json.JSONDecodeError as exc:
                    raise DomainError(f"请求体不是合法 JSON：{exc}") from exc
                if not isinstance(value, dict):
                    raise DomainError("请求体必须是 JSON 对象")
                return value

            # -- 路由 ---------------------------------------------------- #

            def do_GET(self):  # noqa: N802
                self._dispatch("GET")

            def do_POST(self):  # noqa: N802
                self._dispatch("POST")

            def _dispatch(self, method: str) -> None:
                try:
                    actor = self._actor()
                    body = self._body() if method == "POST" else {}
                    path = urlparse(self.path).path.rstrip("/") or "/"
                    result = app.route(method, path, actor, body)
                    _json_response(self, 200, {"ok": True, "data": result})
                except DomainError as exc:
                    _json_response(
                        self, exc.http_status,
                        {"ok": False, "error": {"code": exc.code,
                                                "message": str(exc)}},
                    )
                except Exception as exc:  # noqa: BLE001
                    _json_response(
                        self, 500,
                        {"ok": False, "error": {"code": "internal_error",
                                                "message": str(exc)}},
                    )

        return Handler

    # ------------------------------------------------------------------ #
    # 路由表
    # ------------------------------------------------------------------ #

    def route(self, method: str, path: str, actor: Actor, body: dict) -> dict:
        svc = self.service
        m = re.fullmatch(r"/sessions/([^/]+)/sell", path)
        if method == "POST" and m:
            return svc.sell(
                actor, m.group(1),
                ticket_type_id=_req(body, "ticket_type_id"),
                seats=int(_req(body, "seats")),
                team_id=body.get("team_id"),
                allow_waitlist=bool(body.get("allow_waitlist", True)),
                pending_payment=bool(body.get("pending_payment", False)),
            )
        m = re.fullmatch(r"/sessions/([^/]+)/safety", path)
        if method == "POST" and m:
            return svc.adjust_safety_capacity(
                actor, m.group(1),
                new_capacity=int(_req(body, "new_capacity")),
                reason=str(_req(body, "reason")),
            )
        m = re.fullmatch(r"/sessions/([^/]+)/cancel", path)
        if method == "POST" and m:
            return svc.cancel_session(
                actor, m.group(1), reason=str(_req(body, "reason"))
            )
        m = re.fullmatch(r"/sessions/([^/]+)/free-grants", path)
        if method == "POST" and m:
            return svc.grant_free_quota(
                actor, m.group(1),
                ticket_type_id=_req(body, "ticket_type_id"),
                seats=int(_req(body, "seats")),
                team_id=body.get("team_id"),
            )
        m = re.fullmatch(r"/sessions/([^/]+)/roster", path)
        if method == "GET" and m:
            return svc.roster(actor, m.group(1))
        m = re.fullmatch(r"/sessions/([^/]+)/timeline", path)
        if method == "GET" and m:
            return svc.timeline(actor, m.group(1))
        m = re.fullmatch(r"/sessions/([^/]+)/explain", path)
        if method == "GET" and m:
            return svc.explain_session(actor, m.group(1))
        m = re.fullmatch(r"/bookings/([^/]+)/confirm-payment", path)
        if method == "POST" and m:
            return svc.confirm_payment(actor, m.group(1))
        m = re.fullmatch(r"/bookings/([^/]+)/refund", path)
        if method == "POST" and m:
            return svc.refund(
                actor, m.group(1), reason=str(_req(body, "reason"))
            )
        m = re.fullmatch(r"/bookings/([^/]+)/transfer", path)
        if method == "POST" and m:
            return svc.transfer(
                actor, m.group(1),
                target_session_id=_req(body, "target_session_id"),
                target_ticket_type_id=body.get("target_ticket_type_id"),
            )
        m = re.fullmatch(r"/bookings/([^/]+)/compensation", path)
        if method == "POST" and m:
            return svc.decide_compensation(
                actor, m.group(1),
                arrangement=_req(body, "arrangement"),
                amount=int(body.get("amount", 0)),
                target_session_id=body.get("target_session_id"),
                target_ticket_type_id=body.get("target_ticket_type_id"),
            )
        m = re.fullmatch(r"/bookings/([^/]+)/admit", path)
        if method == "POST" and m:
            return svc.admit(
                actor, m.group(1),
                visitor_name=str(_req(body, "visitor_name")),
                credential=str(_req(body, "credential")),
            )

        if method == "POST" and path == "/venues":
            return svc.create_venue(
                actor, _req(body, "venue_id"), _req(body, "name"),
                _req(body, "region"),
            )
        if method == "POST" and path == "/activities":
            return svc.create_activity(
                actor, _req(body, "activity_id"), _req(body, "title"),
                _req(body, "kind"), _req(body, "organizer"),
            )
        if method == "POST" and path == "/sessions":
            return svc.create_session(
                actor, _req(body, "session_id"), _req(body, "activity_id"),
                _req(body, "venue_id"), _req(body, "start_ts"),
                _req(body, "end_ts"), int(_req(body, "capacity")),
            )
        if method == "POST" and path == "/ticket-types":
            return svc.create_ticket_type(
                actor, _req(body, "ticket_type_id"),
                _req(body, "session_id"), _req(body, "name"),
                int(_req(body, "price")),
                quota=(int(body["quota"]) if body.get("quota") is not None
                       else None),
                is_free=bool(body.get("is_free", False)),
                requires_auth=bool(body.get("requires_auth", False)),
            )
        if method == "POST" and path == "/teams":
            return svc.create_team(
                actor, _req(body, "team_id"), _req(body, "name"),
                contact=body.get("contact"),
                inbound=bool(body.get("inbound", False)),
            )
        if method == "POST" and path == "/recover":
            return svc.recover()
        if method == "GET" and path == "/health":
            return {"status": "ok"}
        raise DomainError(f"未匹配的路由：{method} {path}")


def _req(body: dict, key: str):
    if key not in body or body[key] in (None, ""):
        raise DomainError(f"缺少必填字段：{key}")
    return body[key]


def build_server(service: Service, host: str = "127.0.0.1",
                 port: int = 8080) -> ThreadingHTTPServer:
    app = App(service)

    class _Server(ThreadingHTTPServer):
        daemon_threads = True
        request_queue_size = 512
        allow_reuse_address = True

    return _Server((host, port), app.make_handler())
