"""并发安全测试：多连接、多线程同时售票/退款，容量绝不被突破。"""

import os
import tempfile
import threading
import unittest
from collections import Counter

from src.city_tourism_capacity.models import Actor, TicketKind
from src.city_tourism_capacity.rules import PolicyError
from src.city_tourism_capacity.service import CapacityService
from src.city_tourism_capacity.store import Store


def build_world(path: str) -> CapacityService:
    svc = CapacityService(Store(path))
    svc.create_venue("v_park", "城市公园", "东城区")
    svc.create_session(
        "s_night", "v_park", "中秋夜游园", "游园",
        "2026-09-30T19:00", "2026-09-30T21:30", 50,
        actor=Actor.dispatcher("d1"),
    )
    svc.add_ticket_type("tt_std", "s_night", "标准票", TicketKind.PAID, 50, price=8000)
    return svc


class ConcurrencyTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.NamedTemporaryFile(suffix=".db", delete=False)
        self.tmp.close()
        self.path = self.tmp.name
        build_world(self.path)

    def tearDown(self):
        os.unlink(self.path)
        for suffix in ("-wal", "-shm"):
            p = self.path + suffix
            if os.path.exists(p):
                os.unlink(p)

    def test_concurrent_sales_never_oversell(self):
        # 100 个线程各抢 1 张，容量只有 50；每个线程用独立连接（独立 Store）
        barrier = threading.Barrier(100)
        results: Counter = Counter()
        errors: list[BaseException] = []
        lock = threading.Lock()

        def worker(i: int):
            svc = CapacityService(Store(self.path))
            barrier.wait()
            try:
                r = svc.book(
                    Actor.dispatcher(f"d{i}"), "s_night", "tt_std", 1,
                    visitor_name=f"游客{i}", idem_key=f"sale-{i}",
                )
                with lock:
                    results[r["status"]] += 1
            except BaseException as e:  # noqa: BLE001 - 测试要捕获一切意外
                with lock:
                    errors.append(e)

        threads = [threading.Thread(target=worker, args=(i,)) for i in range(100)]
        for t in threads:
            t.start()
        for t in threads:
            t.join()

        self.assertEqual(errors, [], f"并发售票出现意外异常：{errors[:3]}")
        self.assertEqual(results["confirmed"], 50)
        self.assertEqual(results["waitlisted"], 50)

        # 以数据库事实复核：confirmed 合计 == 安全容量，绝不大于
        svc = CapacityService(Store(self.path))
        with svc.store.read() as c:
            used = c.execute(
                "SELECT COALESCE(SUM(qty),0) n FROM reservations "
                "WHERE session_id='s_night' AND status='confirmed'"
            ).fetchone()["n"]
        self.assertEqual(used, 50)

    def test_concurrent_refunds_release_each_seat_once(self):
        # 先占满 50 张（散客 1..50），再并发：25 人退款 + 50 人抢释放名额
        svc = CapacityService(Store(self.path))
        for i in range(50):
            svc.book(Actor.dispatcher("init"), "s_night", "tt_std", 1,
                     visitor_name=f"持票人{i}", idem_key=f"hold-{i}")

        rids = []
        with svc.store.read() as c:
            for r in c.execute(
                "SELECT reservation_id FROM reservations ORDER BY rowid LIMIT 25"
            ):
                rids.append(r["reservation_id"])

        barrier = threading.Barrier(75)
        errors: list[BaseException] = []
        lock = threading.Lock()
        outcomes: Counter = Counter()

        def refund(i: int):
            s = CapacityService(Store(self.path))
            barrier.wait()
            try:
                rf = s.request_refund(Actor.dispatcher("ref"), rids[i],
                                      "行程变更", f"rf-{i}")
                with lock:
                    outcomes[rf["status"]] += 1
            except BaseException as e:  # noqa: BLE001
                with lock:
                    errors.append(e)

        def grab(i: int):
            s = CapacityService(Store(self.path))
            barrier.wait()
            try:
                r = s.book(Actor.dispatcher("grab"), "s_night", "tt_std", 1,
                           visitor_name=f"补位{i}", idem_key=f"grab-{i}")
                with lock:
                    outcomes[r["status"]] += 1
            except BaseException as e:  # noqa: BLE001
                with lock:
                    errors.append(e)

        threads = [threading.Thread(target=refund, args=(i,)) for i in range(25)]
        threads += [threading.Thread(target=grab, args=(i,)) for i in range(50)]
        for t in threads:
            t.start()
        for t in threads:
            t.join()

        self.assertEqual(errors, [], f"并发退款/补位出现意外异常：{errors[:3]}")
        # 25 笔退款全部完成
        self.assertEqual(outcomes["done"], 25)

        s = CapacityService(Store(self.path))
        with s.store.read() as c:
            counts = {r["status"]: (r["n"], r["q"]) for r in c.execute(
                "SELECT status, COUNT(*) n, COALESCE(SUM(qty),0) q "
                "FROM reservations WHERE session_id='s_night' GROUP BY status"
            ).fetchall()}
            used = c.execute(
                "SELECT COALESCE(SUM(qty),0) n FROM reservations "
                "WHERE session_id='s_night' AND status='confirmed'"
            ).fetchone()["n"]
            refunds = c.execute(
                "SELECT COUNT(*) n FROM refunds WHERE status='done'"
            ).fetchone()["n"]
            dup = c.execute(
                """SELECT reservation_id, COUNT(*) n FROM refunds
                   GROUP BY reservation_id HAVING n > 1"""
            ).fetchall()
        # 无论立即确认还是经候补提升：确认名额始终==安全容量，释放的25个名额
        # 恰好被25张新单占据，剩25张新单候补；退款不重、名额不重复卖出
        self.assertEqual(used, 50)
        self.assertEqual(counts["confirmed"][1], 50)
        self.assertEqual(counts["refunded"][0], 25)
        self.assertEqual(counts["waitlisted"][0], 25)
        self.assertEqual(refunds, 25)
        self.assertEqual(dup, [])

    def test_duplicate_refund_request_is_idempotent(self):
        svc = CapacityService(Store(self.path))
        r = svc.book(Actor.dispatcher("x"), "s_night", "tt_std", 1,
                     visitor_name="甲", idem_key="k1")
        first = svc.request_refund(Actor.dispatcher("x"), r["reservation_id"],
                                   "生病", "idem-rf-1")
        second = svc.request_refund(Actor.dispatcher("x"), r["reservation_id"],
                                    "生病", "idem-rf-1")
        self.assertEqual(first["refund_id"], second["refund_id"])
        # 换幂等键再退同一笔 → 拒绝重复退款
        with self.assertRaisesRegex(PolicyError, r"S09"):
            svc.request_refund(Actor.dispatcher("x"), r["reservation_id"],
                               "生病", "idem-rf-2")


if __name__ == "__main__":
    unittest.main()
