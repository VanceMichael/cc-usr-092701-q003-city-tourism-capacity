"""HTTP API 端到端测试（真实多线程 HTTP 请求）。"""

from __future__ import annotations

import http.client
import json
import tempfile
import threading
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path

from src.city_tourism_capacity.app import build_server
from src.city_tourism_capacity.security import (
    AGENCY,
    DISPATCHER,
    VENUE_STAFF,
    VISITOR_SERVICE,
    Actor,
)
from src.city_tourism_capacity.service import Service
from src.city_tourism_capacity.storage import Storage


def seeded_server():
    path = Path(tempfile.mkdtemp()) / "api.db"
    db = Storage(path)
    base = datetime.now(timezone.utc) + timedelta(days=1)
    svc = Service(db)
    admin = Actor(DISPATCHER, "调度员")
    clerk = Actor(VISITOR_SERVICE, "服务台")
    svc.create_venue(admin, "v_park", "城市公园", "东城区")
    svc.create_activity(admin, "a_show", "实景演出", "演出", "集团")
    svc.create_session(
        admin, "s1", "a_show", "v_park",
        base.isoformat(), (base + timedelta(hours=2)).isoformat(), 5,
    )
    svc.create_ticket_type(admin, "tt1", "s1", "普通票", 100,
                           quota=5)
    server = build_server(svc, port=0)  # 系统分配空闲端口
    port = server.server_address[1]
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    return server, port, db


def request(port, method, path, body=None, headers=None):
    data = json.dumps(body).encode() if body is not None else None
    hdr = {"Content-Type": "application/json"}
    hdr.update(headers or {})
    last_exc: Exception | None = None
    for _ in range(5):  # burst 下 TCP 偶发重置，真实客户端会重连
        try:
            conn = http.client.HTTPConnection("127.0.0.1", port, timeout=10)
            conn.request(method, path, body=data, headers=hdr)
            resp = conn.getresponse()
            raw = resp.read().decode()
            status = resp.status
            conn.close()
            return status, json.loads(raw)
        except (ConnectionError, http.client.HTTPException) as exc:
            last_exc = exc
    raise last_exc  # type: ignore[misc]


def actor_headers(role, name, **extra):
    h = {"X-Actor-Role": role, "X-Actor-Name": name}
    h.update(extra)
    return h


class HttpApiTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.server, cls.port, cls.db = seeded_server()

    @classmethod
    def tearDownClass(cls):
        cls.server.shutdown()
        cls.server.server_close()
        cls.db.close()

    def test_health(self):
        status, body = request(self.port, "GET", "/health",
                               headers=actor_headers(DISPATCHER, "dispatcher-1"))
        self.assertEqual(status, 200)
        self.assertTrue(body["ok"])

    def test_missing_role_rejected(self):
        status, body = request(self.port, "GET", "/health")
        self.assertEqual(status, 403)
        self.assertEqual(body["error"]["code"], "permission_denied")

    def test_sell_and_capacity_conflict(self):
        h = actor_headers(VISITOR_SERVICE, "clerk-1")
        status, body = request(
            self.port, "POST", "/sessions/s1/sell",
            {"ticket_type_id": "tt1", "seats": 5}, h,
        )
        self.assertEqual(status, 200, body)
        self.assertEqual(body["data"]["status"], "confirmed")

        status, body = request(
            self.port, "POST", "/sessions/s1/sell",
            {"ticket_type_id": "tt1", "seats": 1, "allow_waitlist": False},
            h,
        )
        self.assertEqual(status, 409)
        self.assertIn(body["error"]["code"],
                      ("capacity_exceeded", "team_cannot_split"))

    def test_staff_roster_scope(self):
        ok = actor_headers(VENUE_STAFF, "staff-park", **{"X-Venue-Id": "v_park"})
        status, _ = request(self.port, "GET", "/sessions/s1/roster",
                            headers=ok)
        self.assertEqual(status, 200)
        bad = actor_headers(VENUE_STAFF, "staff-other", **{"X-Venue-Id": "v_other"})
        status, body = request(self.port, "GET", "/sessions/s1/roster",
                               headers=bad)
        self.assertEqual(status, 403)

    def test_agency_isolation_over_http(self):
        admin = actor_headers(DISPATCHER, "dispatcher-1")
        # 建第二场次容量 10 与两个旅行社的团队
        base = datetime.now(timezone.utc) + timedelta(days=1)
        request(self.port, "POST", "/venues",
                {"venue_id": "v2", "name": "古建", "region": "西城区"},
                admin)
        request(self.port, "POST", "/activities",
                {"activity_id": "a2", "title": "夜游", "kind": "古建",
                 "organizer": "所"}, admin)
        request(self.port, "POST", "/sessions",
                {"session_id": "s2", "activity_id": "a2", "venue_id": "v2",
                 "start_ts": base.isoformat(),
                 "end_ts": (base + timedelta(hours=2)).isoformat(),
                 "capacity": 10}, admin)
        request(self.port, "POST", "/ticket-types",
                {"ticket_type_id": "tt2", "session_id": "s2", "name": "票",
                 "price": 100, "quota": 10}, admin)
        ha = actor_headers(AGENCY, "agency-a", **{"X-Agency-Id": "ag_a"})
        hb = actor_headers(AGENCY, "agency-b", **{"X-Agency-Id": "ag_b"})
        request(self.port, "POST", "/teams",
                {"team_id": "ta", "name": "A 团"}, ha)
        status, body = request(
            self.port, "POST", "/sessions/s2/sell",
            {"ticket_type_id": "tt2", "seats": 2, "team_id": "ta"}, ha,
        )
        self.assertEqual(status, 200, body)
        bk = body["data"]["booking_id"]
        # B 社不能退 A 社的单
        status, body = request(
            self.port, "POST", f"/bookings/{bk}/refund",
            {"reason": "越权"}, hb,
        )
        self.assertEqual(status, 403)

    def test_concurrent_http_requests_hold_capacity(self):
        # 单独准备一个容量 20 的干净场次
        admin = actor_headers(DISPATCHER, "dispatcher-1")
        clerk = actor_headers(VISITOR_SERVICE, "clerk-1")
        base = datetime.now(timezone.utc) + timedelta(days=1)
        request(self.port, "POST", "/venues",
                {"venue_id": "v3", "name": "游园", "region": "海淀区"}, admin)
        request(self.port, "POST", "/activities",
                {"activity_id": "a3", "title": "灯会", "kind": "游园",
                 "organizer": "公园"}, admin)
        request(self.port, "POST", "/sessions",
                {"session_id": "s3", "activity_id": "a3", "venue_id": "v3",
                 "start_ts": base.isoformat(),
                 "end_ts": (base + timedelta(hours=2)).isoformat(),
                 "capacity": 20}, admin)
        request(self.port, "POST", "/ticket-types",
                {"ticket_type_id": "tt3", "session_id": "s3", "name": "票",
                 "price": 100, "quota": 20}, admin)

        results = []
        errors = []
        barrier = threading.Barrier(40)

        def buy():
            barrier.wait()
            try:
                status, body = request(
                    self.port, "POST", "/sessions/s3/sell",
                    {"ticket_type_id": "tt3", "seats": 1}, clerk,
                )
                if status == 200:
                    results.append(body["data"]["status"])
                else:
                    errors.append((status, body))
            except Exception as exc:  # noqa: BLE001
                errors.append(("exc", str(exc)))

        threads = [threading.Thread(target=buy) for _ in range(40)]
        for t in threads:
            t.start()
        for t in threads:
            t.join()

        self.assertEqual(errors, [])
        confirmed = [s for s in results if s == "confirmed"]
        waiting = [s for s in results if s == "waiting"]
        self.assertEqual(len(confirmed), 20)
        self.assertEqual(len(waiting), 20)
        with self.db.read() as cur:
            cur.execute(
                "SELECT COALESCE(SUM(seats),0) n FROM bookings "
                "WHERE session_id='s3' AND status IN ('pending','confirmed')"
            )
            self.assertEqual(cur.fetchone()["n"], 20)

        # 运营解释视图可读
        status, body = request(self.port, "GET", "/sessions/s3/explain",
                               headers=admin)
        self.assertEqual(status, 200)
        self.assertEqual(body["data"]["session"]["held_seats"], 20)
        self.assertIn("bookings", body["data"])
        self.assertIn("timeline", body["data"])


if __name__ == "__main__":
    unittest.main()
