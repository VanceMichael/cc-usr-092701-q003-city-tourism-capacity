"""核心领域不变量测试。"""

from __future__ import annotations

import tempfile
import threading
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path

from src.city_tourism_capacity.errors import (
    CapacityError,
    ConflictError,
    PermissionDeniedError,
    SessionClosedError,
    TeamAtomicError,
)
from src.city_tourism_capacity.security import (
    AGENCY,
    DISPATCHER,
    VENUE_STAFF,
    VISITOR_SERVICE,
    Actor,
)
from src.city_tourism_capacity.service import Service
from src.city_tourism_capacity.storage import Storage


class Clock:
    def __init__(self, dt: datetime) -> None:
        self.dt = dt

    def __call__(self) -> datetime:
        return self.dt

    def advance(self, **kw) -> None:
        self.dt += timedelta(**kw)


def build_world(path: str | Path = ":memory:"):
    """构造一个标准演示世界并返回 (svc, clock, ids, actors)。"""
    base = datetime(2026, 9, 30, 10, 0, tzinfo=timezone.utc)
    clock = Clock(base)
    db = Storage(path)
    svc = Service(db, now=clock)

    admin = Actor(DISPATCHER, "值班调度员")
    staff_park = Actor(VENUE_STAFF, "公园检票员", venue_id="v_park")
    staff_temple = Actor(VENUE_STAFF, "古建检票员", venue_id="v_temple")
    agency_a = Actor(AGENCY, "A 社计调", agency_id="ag_a")
    agency_b = Actor(AGENCY, "B 社计调", agency_id="ag_b")
    clerk = Actor(VISITOR_SERVICE, "游客服务台")

    svc.create_venue(admin, "v_park", "城市公园", "东城区")
    svc.create_venue(admin, "v_temple", "古建景区", "西城区")
    svc.create_activity(admin, "a_show", "中秋实景演出", "演出", "文旅集团")
    svc.create_activity(admin, "a_yard", "古建夜游", "古建活动", "古建所")
    svc.create_session(
        admin, "s1", "a_show", "v_park",
        base.isoformat(), (base + timedelta(hours=2)).isoformat(), 10,
    )
    svc.create_session(
        admin, "s2", "a_yard", "v_temple",
        base.isoformat(), (base + timedelta(hours=2)).isoformat(), 10,
    )
    svc.create_ticket_type(admin, "tt_s1", "s1", "普通票", 12000, quota=10)
    svc.create_ticket_type(
        admin, "tt_free_s1", "s1", "入境团队免费票", 0,
        quota=10, is_free=True, requires_auth=True,
    )
    svc.create_ticket_type(admin, "tt_s2", "s2", "普通票", 8000, quota=10)
    ids = {"s1": "s1", "s2": "s2", "tt_s1": "tt_s1",
           "tt_free_s1": "tt_free_s1", "tt_s2": "tt_s2"}
    actors = {
        "admin": admin, "staff_park": staff_park,
        "staff_temple": staff_temple, "agency_a": agency_a,
        "agency_b": agency_b, "clerk": clerk,
    }
    return svc, clock, ids, actors, db


class ConcurrencyTest(unittest.TestCase):
    def test_concurrent_sell_never_oversells(self):
        svc, _, ids, actors, db = build_world()
        results: list[str] = []
        errors: list[Exception] = []
        barrier = threading.Barrier(30)

        def sell_one(i):
            barrier.wait()
            try:
                r = svc.sell(actors["clerk"], ids["s1"], ids["tt_s1"], 1)
                results.append(r["status"])
            except Exception as exc:  # noqa: BLE001
                errors.append(exc)

        threads = [threading.Thread(target=sell_one, args=(i,))
                   for i in range(30)]
        for t in threads:
            t.start()
        for t in threads:
            t.join()

        self.assertEqual(errors, [])
        confirmed = [s for s in results if s == "confirmed"]
        waiting = [s for s in results if s == "waiting"]
        self.assertEqual(len(confirmed), 10)
        self.assertEqual(len(waiting), 20)
        with db.read() as cur:
            cur.execute(
                "SELECT COALESCE(SUM(seats),0) n FROM bookings "
                "WHERE session_id='s1' AND status IN ('pending','confirmed')"
            )
            held = cur.fetchone()["n"]
        self.assertEqual(held, 10)

    def test_refund_releases_and_waitlist_fills_without_resale(self):
        svc, _, ids, actors, db = build_world()
        # 售出 10 席（7+3 两个团队），再有 3 人团队与 2 人团队候补
        b1 = svc.sell(actors["clerk"], "s1", "tt_s1", 7)["booking_id"]
        svc.sell(actors["clerk"], "s1", "tt_s1", 3)
        w3 = svc.sell(actors["clerk"], "s1", "tt_s1", 3)
        w2 = svc.sell(actors["clerk"], "s1", "tt_s1", 2)
        self.assertEqual(w3["status"], "waiting")
        self.assertEqual(w2["status"], "waiting")

        # 退 7 席：释放与递补在同一事务内完成
        out = svc.refund(actors["clerk"], b1, "行程变更")
        promoted = {p["booking_id"] for p in out["promoted"]}
        # 3 人团队先整体递补（占 3），2 人团队随后整体递补（再占 2），共 5，
        # 剩余 2 席不会被再次卖出——候补队列里已无更小团队
        self.assertIn(w3["booking_id"], promoted)
        self.assertIn(w2["booking_id"], promoted)
        with db.read() as cur:
            cur.execute(
                "SELECT COALESCE(SUM(seats),0) n FROM bookings "
                "WHERE session_id='s1' AND status IN ('pending','confirmed')"
            )
            self.assertLessEqual(cur.fetchone()["n"], 10)
            cur.execute("SELECT status FROM bookings WHERE booking_id=?",
                        (w3["booking_id"],))
            self.assertEqual(cur.fetchone()["status"], "confirmed")

    def test_concurrent_refund_and_sell_holds_capacity(self):
        svc, _, ids, actors, db = build_world()
        full = [svc.sell(actors["clerk"], "s1", "tt_s1", 1)["booking_id"]
                for _ in range(10)]
        # 20 个并发请求在容量满时全部候补
        late = []
        for _ in range(20):
            late.append(svc.sell(actors["clerk"], "s1", "tt_s1", 1)
                        ["booking_id"])

        def churn():
            for bid in full:
                try:
                    svc.refund(actors["clerk"], bid, "退")
                except ConflictError:
                    pass

        t = threading.Thread(target=churn)
        t.start()
        t.join()
        with db.read() as cur:
            cur.execute(
                "SELECT COALESCE(SUM(seats),0) n FROM bookings "
                "WHERE session_id='s1' AND status IN ('pending','confirmed')"
            )
            held = cur.fetchone()["n"]
        self.assertLessEqual(held, 10)


class TeamAtomicityTest(unittest.TestCase):
    def test_team_never_split_across_cap(self):
        svc, _, ids, actors, db = build_world()
        svc.sell(actors["clerk"], "s1", "tt_s1", 4)
        svc.sell(actors["clerk"], "s1", "tt_s1", 3)
        svc.sell(actors["clerk"], "s1", "tt_s1", 3)  # 恰好 10
        # 一个 2 人团队无法整体放入 → 整体候补，而不是确认 0/1 人
        w = svc.sell(actors["clerk"], "s1", "tt_s1", 2)
        self.assertEqual(w["status"], "waiting")

    def test_safety_reduction_moves_whole_teams(self):
        svc, _, ids, actors, db = build_world()
        svc.sell(actors["clerk"], "s1", "tt_s1", 4)
        b2 = svc.sell(actors["clerk"], "s1", "tt_s1", 3)["booking_id"]
        b3 = svc.sell(actors["clerk"], "s1", "tt_s1", 3)["booking_id"]
        out = svc.adjust_safety_capacity(
            actors["admin"], "s1", 5, "临时围挡占用通道"
        )
        displaced = {d["booking_id"] for d in out["displaced"]}
        # 4 人团队保留；两个 3 人团队必须整体移出（不能只移出 1 人）
        self.assertEqual(displaced, {b2, b3})
        with db.read() as cur:
            cur.execute(
                "SELECT COALESCE(SUM(seats),0) n FROM bookings "
                "WHERE session_id='s1' AND status IN ('pending','confirmed')"
            )
            self.assertEqual(cur.fetchone()["n"], 4)

    def test_transfer_refuses_partial_fit(self):
        svc, _, ids, actors, _ = build_world()
        svc.sell(actors["clerk"], "s1", "tt_s1", 8)
        svc.create_team(actors["agency_a"], "tm1", "入境团 A", inbound=True)
        bk = svc.sell(actors["agency_a"], "s1", "tt_s1", 2,
                      team_id="tm1")["booking_id"]
        # s2 只有 10 席，转入一个 2 人团可以；先占 9 席后应拒绝整体转入
        svc.sell(actors["clerk"], "s2", "tt_s2", 9)
        with self.assertRaises(TeamAtomicError):
            svc.transfer(actors["admin"], bk, "s2", "tt_s2")


class LateReceiptTest(unittest.TestCase):
    def test_late_payment_receipt_does_not_reopen_session(self):
        svc, clock, ids, actors, db = build_world()
        bk = svc.sell(actors["clerk"], "s1", "tt_s1", 2,
                      pending_payment=True)["booking_id"]
        clock.advance(hours=3)  # 场次已结束
        with self.assertRaises(SessionClosedError):
            svc.confirm_payment(actors["clerk"], bk)
        with db.read() as cur:
            cur.execute("SELECT status FROM bookings WHERE booking_id=?",
                        (bk,))
            self.assertEqual(cur.fetchone()["status"], "cancelled")
            cur.execute("SELECT COUNT(*) n FROM refunds r JOIN bookings b "
                        "ON r.booking_id=b.booking_id WHERE b.booking_id=?",
                        (bk,))
            self.assertEqual(cur.fetchone()["n"], 1)
            cur.execute("SELECT status FROM sessions WHERE session_id='s1'")
            self.assertEqual(cur.fetchone()["status"], "finished")
        # 回执重发仍然拒绝，场次不会复活
        with self.assertRaises(SessionClosedError):
            svc.confirm_payment(actors["clerk"], bk)
        # 结束后的入场/补录同样拒绝
        with self.assertRaises(SessionClosedError):
            svc.admit(actors["staff_park"], bk, "张三", "IDCARD-1")

    def test_cancelled_session_rejects_all_writes(self):
        svc, _, ids, actors, _ = build_world()
        svc.cancel_session(actors["admin"], "s1", "极端天气预警")
        with self.assertRaises(SessionClosedError):
            svc.sell(actors["clerk"], "s1", "tt_s1", 1, allow_waitlist=False)
        with self.assertRaises(SessionClosedError):
            svc.adjust_safety_capacity(actors["admin"], "s1", 20, "恢复")


class PermissionTest(unittest.TestCase):
    def test_venue_staff_scoped_to_own_venue(self):
        svc, _, ids, actors, _ = build_world()
        own = svc.roster(actors["staff_park"], "s1")
        self.assertEqual(own["session_id"], "s1")
        with self.assertRaises(PermissionDeniedError):
            svc.roster(actors["staff_park"], "s2")  # 其它景区名单不可见
        with self.assertRaises(PermissionDeniedError):
            svc.adjust_safety_capacity(actors["staff_park"], "s1", 8, "x")
        with self.assertRaises(PermissionDeniedError):
            svc.timeline(actors["staff_park"], "s1")

    def test_agency_only_modifies_own_teams(self):
        svc, _, ids, actors, _ = build_world()
        svc.create_team(actors["agency_a"], "ta", "A 社团队")
        svc.create_team(actors["agency_b"], "tb", "B 社团队")
        bk = svc.sell(actors["agency_a"], "s1", "tt_s1", 2,
                      team_id="ta")["booking_id"]
        # B 社不能退 A 社的单，也不能借 A 社团队下单
        with self.assertRaises(PermissionDeniedError):
            svc.refund(actors["agency_b"], bk, "恶意退款")
        with self.assertRaises(PermissionDeniedError):
            svc.sell(actors["agency_b"], "s1", "tt_s1", 1, team_id="ta")
        # A 社自己可以操作
        svc.refund(actors["agency_a"], bk, "行程取消")

    def test_free_quota_requires_authorization(self):
        svc, _, ids, actors, _ = build_world()
        svc.create_team(actors["admin"], "inbound1", "入境团", inbound=True)
        # 未授权：游客服务台与旅行社都拿不到免费票
        with self.assertRaises(PermissionDeniedError):
            svc.sell(actors["clerk"], "s1", "tt_free_s1", 3,
                     team_id="inbound1")
        with self.assertRaises(PermissionDeniedError):
            svc.grant_free_quota(actors["clerk"], "s1", "tt_free_s1", 3)
        # 调度员授权后才能出票
        svc.grant_free_quota(actors["admin"], "s1", "tt_free_s1", 3,
                             team_id="inbound1")
        r = svc.sell(actors["clerk"], "s1", "tt_free_s1", 3,
                     team_id="inbound1")
        self.assertEqual(r["status"], "confirmed")

    def test_team_specific_grant_not_usable_by_other_team(self):
        svc, _, ids, actors, _ = build_world()
        svc.create_team(actors["admin"], "g1", "入境团 1", inbound=True)
        svc.create_team(actors["admin"], "g2", "入境团 2", inbound=True)
        svc.grant_free_quota(actors["admin"], "s1", "tt_free_s1", 2,
                             team_id="g1")
        with self.assertRaises(PermissionDeniedError):
            svc.sell(actors["clerk"], "s1", "tt_free_s1", 2, team_id="g2")
        svc.sell(actors["clerk"], "s1", "tt_free_s1", 2, team_id="g1")

    def test_cross_region_requires_dispatcher(self):
        svc, _, ids, actors, _ = build_world()
        # 再造一个跨区场地与场次
        svc.create_venue(actors["admin"], "v_suburb", "密云农庄", "密云区")
        svc.create_activity(actors["admin"], "a_harvest", "丰收节", "节庆",
                            "镇政府")
        base = datetime(2026, 9, 30, 10, 0, tzinfo=timezone.utc)
        svc.create_session(
            actors["admin"], "s3", "a_harvest", "v_suburb",
            base.isoformat(), (base + timedelta(hours=2)).isoformat(), 50,
        )
        svc.create_ticket_type(actors["admin"], "tt_s3", "s3", "入场券",
                               5000, quota=50)
        svc.create_team(actors["agency_a"], "treg", "A 社郊区团")
        bk = svc.sell(actors["agency_a"], "s1", "tt_s1", 2,
                      team_id="treg")["booking_id"]
        with self.assertRaises(PermissionDeniedError):
            svc.transfer(actors["agency_a"], bk, "s3", "tt_s3")
        out = svc.transfer(actors["admin"], bk, "s3", "tt_s3")
        self.assertEqual(out["status"], "confirmed")


class RecoveryTest(unittest.TestCase):
    def test_restart_settles_pending_refunds(self):
        tmp = Path(tempfile.mkdtemp()) / "t.db"
        svc, _, ids, actors, db = build_world(tmp)
        # 支付网关第一次调用失败：退款停在 pending
        svc._gateway = lambda rid, amount: False
        bk = svc.sell(actors["clerk"], "s1", "tt_s1", 2)["booking_id"]
        out = svc.refund(actors["clerk"], bk, "支付渠道抖动")
        self.assertEqual(out["status"], "pending")
        db.close()

        # 模拟服务重启：新实例、网关恢复，recover 继续未完成退款
        db2 = Storage(tmp)
        svc2 = Service(db2)
        rec = svc2.recover()
        self.assertEqual(len(rec["refunds_settled"]), 1)
        with db2.read() as cur:
            cur.execute("SELECT status FROM refunds")
            self.assertTrue(all(r["status"] == "done"
                                for r in cur.fetchall()))
        db2.close()

    def test_restart_continues_waitlist(self):
        tmp = Path(tempfile.mkdtemp()) / "t2.db"
        svc, clock, ids, actors, db = build_world(tmp)
        svc.sell(actors["clerk"], "s1", "tt_s1", 7)
        # 直接构造“崩溃后”残留状态：一张候补单 + 3 席空位
        ts = clock.dt.isoformat()
        with db.tx() as cur:
            cur.execute(
                "INSERT INTO teams VALUES ('tm_crash',NULL,'崩溃残留团',0,"
                "NULL,?)", (ts,),
            )
            cur.execute(
                "INSERT INTO bookings (booking_id,session_id,team_id,"
                "ticket_type_id,seats,status,price_each,total_amount,"
                "created_ts,updated_ts) VALUES "
                "('bk_crash','s1','tm_crash','tt_s1',3,'waiting',12000,"
                "36000,?,?)",
                (ts, ts),
            )
        db.close()

        db2 = Storage(tmp)
        svc2 = Service(db2, now=clock)
        rec = svc2.recover()
        self.assertEqual(
            [(p["booking_id"], p["seats"]) for p in rec["waitlist_promoted"]],
            [("bk_crash", 3)],
        )
        with db2.read() as cur:
            cur.execute("SELECT status FROM bookings WHERE booking_id='bk_crash'")
            self.assertEqual(cur.fetchone()["status"], "confirmed")
        db2.close()

    def test_recover_does_not_reopen_finished_session(self):
        tmp = Path(tempfile.mkdtemp()) / "t3.db"
        svc, clock, ids, actors, db = build_world(tmp)
        bk = svc.sell(actors["clerk"], "s1", "tt_s1", 2,
                      pending_payment=True)["booking_id"]
        db.close()
        clock.advance(hours=3)

        db2 = Storage(tmp)
        svc2 = Service(db2, now=clock)
        svc2.recover()
        with db2.read() as cur:
            cur.execute("SELECT status FROM sessions WHERE session_id='s1'")
            self.assertEqual(cur.fetchone()["status"], "finished")
            cur.execute("SELECT status FROM bookings WHERE booking_id=?",
                        (bk,))
            self.assertEqual(cur.fetchone()["status"], "cancelled")
        with self.assertRaises(SessionClosedError):
            svc2.confirm_payment(actors["clerk"], bk)
        db2.close()


class TimelineExplainTest(unittest.TestCase):
    def test_single_timeline_records_every_decision(self):
        svc, _, ids, actors, _ = build_world()
        svc.create_team(actors["clerk"], "tm", "某旅行团")
        bk = svc.sell(actors["clerk"], "s1", "tt_s1", 4,
                      team_id="tm")["booking_id"]
        svc.adjust_safety_capacity(actors["admin"], "s1", 3, "消防通道整改")
        svc.decide_compensation(actors["admin"], bk, "voucher", amount=5000)

        tl = svc.timeline(actors["admin"], "s1")
        kinds = [e["event_type"] for e in tl["events"]]
        self.assertEqual(kinds[0], "session_created")
        self.assertIn("safety_adjusted", kinds)
        self.assertIn("compensation_decided", kinds)

        view = svc.explain_session(actors["admin"], "s1")
        self.assertEqual(view["session"]["safety_capacity"], 3)
        self.assertIn("safety_capacity_reduced", view["rules_triggered"])
        self.assertIn("compensation_decided", view["rules_triggered"])
        explained = next(b for b in view["bookings"]
                         if b["booking_id"] == bk)
        self.assertTrue(explained["why"].startswith("补偿决定"))
        self.assertIn("voucher:5000", explained["why"])
        self.assertGreaterEqual(len(explained["decisions"]), 2)

    def test_cancel_refunds_waitlist_and_explains(self):
        svc, _, ids, actors, db = build_world()
        b1 = svc.sell(actors["clerk"], "s1", "tt_s1", 9)["booking_id"]
        w = svc.sell(actors["clerk"], "s1", "tt_s1", 2)  # 放不下 → 候补
        self.assertEqual(w["status"], "waiting")
        svc.cancel_session(actors["admin"], "s1", "暴雨橙色预警")
        with db.read() as cur:
            cur.execute("SELECT status FROM bookings WHERE booking_id=?",
                        (b1,))
            self.assertEqual(cur.fetchone()["status"], "refunded")
            cur.execute("SELECT status FROM bookings WHERE booking_id=?",
                        (w["booking_id"],))
            self.assertEqual(cur.fetchone()["status"], "cancelled")
        view = svc.explain_session(actors["admin"], "s1")
        self.assertIn("session_cancelled", view["rules_triggered"])

    def test_transfer_appears_on_both_timelines(self):
        svc, _, ids, actors, _ = build_world()
        bk = svc.sell(actors["clerk"], "s1", "tt_s1", 2)["booking_id"]
        svc.transfer(actors["admin"], bk, "s2", "tt_s2")
        t1 = [e["event_type"] for e in svc.timeline(actors["admin"], "s1")
              ["events"]]
        t2 = [e["event_type"] for e in svc.timeline(actors["admin"], "s2")
              ["events"]]
        self.assertIn("transferred_out", t1)
        self.assertIn("transferred_in", t2)


class AdmissionTest(unittest.TestCase):
    def test_checkin_counts_and_duplicate_credential(self):
        svc, _, ids, actors, _ = build_world()
        bk = svc.sell(actors["clerk"], "s1", "tt_s1", 2)["booking_id"]
        svc.admit(actors["staff_park"], bk, "张三", "ID-1")
        again = svc.admit(actors["staff_park"], bk, "张三", "ID-1")
        self.assertEqual(again["status"], "already_admitted")
        svc.admit(actors["staff_park"], bk, "李四", "ID-2")
        with self.assertRaises(ConflictError):
            svc.admit(actors["staff_park"], bk, "王五", "ID-3")
        roster = svc.roster(actors["staff_park"], "s1")
        row = next(b for b in roster["bookings"]
                   if b["booking_id"] == bk)
        self.assertEqual(row["admitted"], 2)

    def test_other_venue_staff_cannot_checkin(self):
        svc, _, ids, actors, _ = build_world()
        bk = svc.sell(actors["clerk"], "s1", "tt_s1", 1)["booking_id"]
        with self.assertRaises(PermissionDeniedError):
            svc.admit(actors["staff_temple"], bk, "张三", "ID-1")


if __name__ == "__main__":
    unittest.main()
