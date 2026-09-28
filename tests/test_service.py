"""领域规则测试：整团原子、权限隔离、限流/取消/转场/补偿、
迟到回执不重开、崩溃恢复、现场核验与解释视图。"""

import os
import tempfile
import unittest

from src.city_tourism_capacity.models import Actor, TicketKind
from src.city_tourism_capacity.rules import PolicyError
from src.city_tourism_capacity.service import CapacityService
from src.city_tourism_capacity.store import Store


class ServiceTest(unittest.TestCase):
    def setUp(self):
        self.svc = CapacityService(Store(":memory:"))
        s = self.svc
        # 东城区：夜游园（容量 40，标准票 40 + 免费票 0 池）
        s.create_venue("v_dong", "地坛公园", "东城区")
        s.create_session("s_a", "v_dong", "中秋夜游园", "游园",
                         "2026-09-30T19:00", "2026-09-30T21:30", 40,
                         actor=Actor.dispatcher("d1"))
        s.add_ticket_type("tt_a", "s_a", "标准票", TicketKind.PAID, 40, price=8000)
        s.add_ticket_type("tt_free_a", "s_a", "公益免费票", TicketKind.FREE, 0)
        # 同区备选同类场次
        s.create_session("s_b", "v_dong", "国庆夜游园", "游园",
                         "2026-10-02T19:00", "2026-10-02T21:30", 40,
                         actor=Actor.dispatcher("d1"))
        s.add_ticket_type("tt_b", "s_b", "标准票", TicketKind.PAID, 40, price=8000)
        # 怀柔区：京郊丰收节（跨区转场目标）
        s.create_venue("v_huairou", "雁栖镇", "怀柔区")
        s.create_session("s_c", "v_huairou", "京郊丰收节", "节庆",
                         "2026-10-01T10:00", "2026-10-01T16:00", 100,
                         actor=Actor.dispatcher("d1"))
        s.add_ticket_type("tt_c", "s_c", "标准票", TicketKind.PAID, 100, price=5000)

        s.register_team("team_x", "CITS", "王领队", 12, "13800000001")
        s.register_team("team_y", "OTHER", "李领队", 30, "13800000002")

        self.dispatcher = Actor.dispatcher("d1")
        self.cits = Actor.agency("a_cits", "CITS")
        self.other = Actor.agency("a_other", "OTHER")
        self.staff_a = Actor.site_staff("staff_a", "s_a")
        self.staff_b = Actor.site_staff("staff_b", "s_b")

    # ---- 整团原子 -------------------------------------------------------
    def test_team_booking_is_atomic(self):
        # 容量 40：30 人团队先占，剩 10；12 人团队整团放不下 → 全团候补，不进 10 个
        self.svc.book(self.other, "s_a", "tt_a", 30, visitor_name="李领队",
                      team_id="team_y", idem_key="y1")
        r = self.svc.book(self.cits, "s_a", "tt_a", 12, visitor_name="王领队",
                          team_id="team_x", idem_key="x1")
        self.assertEqual(r["status"], "waitlisted")
        with self.svc.store.read() as c:
            n = c.execute(
                "SELECT COUNT(*) n FROM reservations WHERE team_id='team_x'"
            ).fetchone()["n"]
            used = c.execute(
                "SELECT COALESCE(SUM(qty),0) n FROM reservations "
                "WHERE session_id='s_a' AND status='confirmed'"
            ).fetchone()["n"]
        self.assertEqual(n, 1)  # 整团只有一条预约
        self.assertEqual(used, 30)  # 没有任何成员越过安全上限

    def test_team_qty_must_equal_size(self):
        with self.assertRaisesRegex(PolicyError, r"S01"):
            self.svc.book(self.cits, "s_a", "tt_a", 10, visitor_name="王领队",
                          team_id="team_x", idem_key="x2")

    # ---- 权限隔离 -------------------------------------------------------
    def test_agency_can_only_touch_own_team(self):
        self.svc.book(self.cits, "s_a", "tt_a", 12, visitor_name="王领队",
                      team_id="team_x", idem_key="own1")
        with self.assertRaisesRegex(PolicyError, r"S16"):
            self.svc.book(self.other, "s_a", "tt_a", 12, visitor_name="王领队",
                          team_id="team_x", idem_key="steal1")
        # 旅行社不能代散客下单
        with self.assertRaisesRegex(PolicyError, r"S16"):
            self.svc.book(self.cits, "s_a", "tt_a", 1, visitor_name="散客",
                          idem_key="retail")

    def test_site_staff_scoped_to_own_session(self):
        r = self.svc.book(self.dispatcher, "s_a", "tt_a", 1,
                          visitor_name="甲", idem_key="p1")
        # 本场工作人员可核验
        self.svc.check_in(self.staff_a, r["reservation_id"])
        # 别的场次的工作人员不能核验、也不能看名单
        with self.assertRaisesRegex(PolicyError, r"S19"):
            self.svc.check_in(self.staff_b, r["reservation_id"])
        with self.assertRaisesRegex(PolicyError, r"S15"):
            self.svc.session_roster(self.staff_b, "s_a")
        roster = self.svc.session_roster(self.staff_a, "s_a")
        self.assertEqual(len(roster), 1)
        # 重复核验拒绝（每人一次）
        with self.assertRaisesRegex(PolicyError, r"S18"):
            self.svc.check_in(self.staff_a, r["reservation_id"])

    def test_free_quota_requires_authorization(self):
        # 无授权：旅行社签免费票（整团 12 人）被拒
        with self.assertRaisesRegex(PolicyError, r"S02"):
            self.svc.book(self.cits, "s_a", "tt_free_a", 12,
                          visitor_name="公益团", team_id="team_x", idem_key="f1")
        # 调度员授权该旅行社，并把免费票池扩到 15（免费名额单列，不与售票混算）
        self.svc.grant_authorization(self.dispatcher, "a_cits", "free_quota", "s_a",
                                     {"purpose": "社区公益团"})
        with self.svc.store.write() as c:
            c.execute("UPDATE ticket_types SET total_qty=15 WHERE type_id='tt_free_a'")
        r2 = self.svc.book(self.cits, "s_a", "tt_free_a", 12,
                           visitor_name="公益团", team_id="team_x", idem_key="f2")
        self.assertEqual(r2["status"], "confirmed")
        with self.svc.store.read() as c:
            paid_used = c.execute(
                "SELECT COALESCE(SUM(qty),0) n FROM reservations "
                "WHERE type_id='tt_a' AND status='confirmed'"
            ).fetchone()["n"]
            free_used = c.execute(
                "SELECT COALESCE(SUM(qty),0) n FROM reservations "
                "WHERE type_id='tt_free_a' AND status='confirmed'"
            ).fetchone()["n"]
        self.assertEqual(paid_used, 0)
        self.assertEqual(free_used, 12)

    def test_capacity_adjust_is_dispatcher_only(self):
        with self.assertRaisesRegex(PolicyError, r"S17"):
            self.svc.adjust_safe_capacity(self.cits, "s_a", 20, "临时限流")

    # ---- 限流、挤出、候补递补 -------------------------------------------
    def test_capacity_cut_displaces_latest_and_feeds_waitlist(self):
        # 占满 40：30 人团 + 10 张散客
        self.svc.book(self.other, "s_a", "tt_a", 30, visitor_name="李领队",
                      team_id="team_y", idem_key="t30")
        for i in range(10):
            self.svc.book(self.dispatcher, "s_a", "tt_a", 1,
                          visitor_name=f"散客{i}", idem_key=f"w{i}")
        # 再来一个 8 人小团候补
        self.svc.register_team("team_z", "CITS", "赵领队", 8, "13800000003")
        self.svc.book(self.cits, "s_a", "tt_a", 8, visitor_name="赵领队",
                      team_id="team_z", idem_key="t8",
                      preference={"accept_reroll": False})

        # 限流到 38：最晚确认的 2 个散客被挤出并排队退款；8 人团仍放不下
        result = self.svc.adjust_safe_capacity(self.dispatcher, "s_a", 38, "舞台搭建占用")
        self.assertEqual(len(result["displaced"]), 2)
        self.assertEqual(result["rerolled"], [])
        self.assertEqual(result["waitlist_promoted"], [])

        # 恢复处理退款 outbox → 2 名额释放；30 人团 + 8 散客 = 38，8 人团仍放不下
        resumed = self.svc.resume_pending()
        self.assertEqual(len(resumed["refunds_processed"]), 2)
        with self.svc.store.read() as c:
            used = c.execute(
                "SELECT COALESCE(SUM(qty),0) n FROM reservations "
                "WHERE session_id='s_a' AND status='confirmed'"
            ).fetchone()["n"]
            z = c.execute(
                "SELECT status FROM reservations WHERE team_id='team_z'"
            ).fetchone()["status"]
        self.assertEqual(used, 38)
        self.assertEqual(z, "waitlisted")

        # 剩余 8 张散客全部退款 → 恰好腾出 8 席 → 8 人团按 FIFO 整团确认
        retail = []
        with self.svc.store.read() as c:
            retail = [r["reservation_id"] for r in c.execute(
                "SELECT reservation_id FROM reservations WHERE team_id IS NULL "
                "AND status='confirmed' ORDER BY confirmed_at")]
        self.assertEqual(len(retail), 8)
        for j, rid in enumerate(retail):
            self.svc.request_refund(self.dispatcher, rid, "个人原因", f"wr{j}")
        with self.svc.store.read() as c:
            z = c.execute(
                "SELECT status FROM reservations WHERE team_id='team_z'"
            ).fetchone()["status"]
            used = c.execute(
                "SELECT COALESCE(SUM(qty),0) n FROM reservations "
                "WHERE session_id='s_a' AND status='confirmed'"
            ).fetchone()["n"]
        self.assertEqual(z, "confirmed")
        self.assertEqual(used, 38)  # 30 人团 + 8 人候补团，顶满但不超限

    def test_capacity_can_restore_to_original_and_promotes_waitlist(self):
        # 30 人团占满 30（容量 40 还剩 10），12 人团候补
        self.svc.book(self.other, "s_a", "tt_a", 30, visitor_name="李领队",
                      team_id="team_y", idem_key="r30")
        waiter = self.svc.book(self.cits, "s_a", "tt_a", 12, visitor_name="王领队",
                               team_id="team_x", idem_key="r12")
        self.assertEqual(waiter["status"], "waitlisted")
        # 限流到 20 → 挤出最晚确认（30 人团中无法拆团，整条挤出排队退款）
        cut = self.svc.adjust_safe_capacity(self.dispatcher, "s_a", 20, "临时管控")
        self.assertEqual(len(cut["displaced"]), 1)
        self.svc.resume_pending()
        # 管控解除，恢复到原始容量 40（不能超过）
        restored = self.svc.adjust_safe_capacity(self.dispatcher, "s_a", 40, "管控解除")
        self.assertTrue(restored["changed"])
        with self.assertRaisesRegex(PolicyError, r"S03"):
            self.svc.adjust_safe_capacity(self.dispatcher, "s_a", 41, "试图超售")
        # 恢复后候补 12 人团被 FIFO 唤醒（被挤出且退款的 30 人团不自动回填，已退款）
        with self.svc.store.read() as c:
            used = c.execute(
                "SELECT COALESCE(SUM(qty),0) n FROM reservations "
                "WHERE session_id='s_a' AND status='confirmed'"
            ).fetchone()["n"]
            x = c.execute("SELECT status FROM reservations WHERE team_id='team_x'"
                          ).fetchone()["status"]
        self.assertEqual(x, "confirmed")
        self.assertEqual(used, 12)

    # ---- 取消 / 转场 / 补偿 ---------------------------------------------
    def test_cancel_refunds_or_rerolls_with_preference(self):
        # 12 人团愿意接受同区同类转场；1 散客无偏好
        team_res = self.svc.book(self.cits, "s_a", "tt_a", 12,
                                 visitor_name="王领队", team_id="team_x",
                                 idem_key="tx12",
                                 preference={"accept_reroll": True,
                                             "same_activity_type": True})
        solo = self.svc.book(self.dispatcher, "s_a", "tt_a", 1,
                             visitor_name="散客丙", idem_key="solo1")
        result = self.svc.cancel_session(self.dispatcher, "s_a", "暴雨红色预警")
        # 团队转入 s_b；散客排队退款
        self.assertEqual(len(result["rerolled"]), 1)
        self.assertEqual(result["rerolled"][0]["target"], "s_b")
        self.assertEqual(len(result["refunds_queued"]), 1)

        with self.svc.store.read() as c:
            old = c.execute("SELECT status FROM reservations WHERE reservation_id=?",
                            (team_res["reservation_id"],)).fetchone()["status"]
            nb = c.execute(
                "SELECT status, qty FROM reservations WHERE reroll_from=?",
                (team_res["reservation_id"],)).fetchone()
            state = c.execute("SELECT state FROM sessions WHERE session_id='s_a'"
                              ).fetchone()["state"]
        self.assertEqual(old, "rerolled")
        self.assertEqual(nb["status"], "confirmed")
        self.assertEqual(nb["qty"], 12)
        self.assertEqual(state, "cancelled")

        # 散客退款恢复后完成；调度员补发代金券补偿并留痕
        self.svc.resume_pending()
        comp = self.svc.decide_compensation(self.dispatcher, solo["reservation_id"],
                                            "voucher", amount=5000,
                                            note="暴雨取消代金券")
        self.assertTrue(comp.startswith("comp_"))

    def test_cross_region_reroll_requires_authorization(self):
        r = self.svc.book(self.cits, "s_a", "tt_a", 12, visitor_name="王领队",
                          team_id="team_x", idem_key="xr1")
        # 未授权跨区 → S17
        with self.assertRaisesRegex(PolicyError, r"S17"):
            self.svc.reroll_reservation(self.cits, r["reservation_id"], "s_c")
        # 同区 s_b 不需要跨区授权即可转
        new_id = self.svc.reroll_reservation(self.cits, r["reservation_id"], "s_b")
        self.assertTrue(new_id)
        with self.svc.store.read() as c:
            self.assertEqual(
                c.execute("SELECT status FROM reservations WHERE reservation_id=?",
                          (r["reservation_id"],)).fetchone()["status"],
                "rerolled")

    # ---- 终态冻结：迟到回执不能重开 -------------------------------------
    def test_late_ack_after_finish_is_rejected_and_recorded(self):
        r = self.svc.book(self.dispatcher, "s_a", "tt_a", 1,
                          visitor_name="迟到游客", idem_key="late1")
        self.svc.finish_session(self.dispatcher, "s_a")
        # 场次前一天已结束，渠道退款回执第二天才到（由调度员代渠道落地）
        ack = self.svc.request_refund(self.dispatcher, r["reservation_id"],
                                      "渠道迟到回执", "late-1")
        self.assertEqual(ack["status"], "rejected")
        with self.svc.store.read() as c:
            state = c.execute("SELECT state FROM sessions WHERE session_id='s_a'"
                              ).fetchone()["state"]
            rejected = c.execute(
                "SELECT COUNT(*) n FROM timeline WHERE rule='S12'"
            ).fetchone()["n"]
            still_confirmed = c.execute(
                "SELECT status FROM reservations WHERE reservation_id=?",
                (r["reservation_id"],)).fetchone()["status"]
        self.assertEqual(state, "finished")
        self.assertGreaterEqual(rejected, 1)
        self.assertEqual(still_confirmed, "confirmed")  # 终态预约不被迟到回执改动
        # 已结束场次也不能核验
        with self.assertRaisesRegex(PolicyError, r"S20"):
            self.svc.check_in(self.staff_a, r["reservation_id"])

    # ---- 崩溃恢复：未完成候补与退款继续处理 -----------------------------
    def test_resume_continues_pending_work(self):
        tmp = tempfile.NamedTemporaryFile(suffix=".db", delete=False)
        tmp.close()
        path = tmp.name
        try:
            svc = CapacityService(Store(path))
            svc.create_venue("v1", "园", "东城区")
            svc.create_session("s1", "v1", "演出", "演出",
                               "2026-09-30T19:00", "2026-09-30T21:00", 3,
                               actor=Actor.dispatcher("d"))
            svc.add_ticket_type("t1", "s1", "票", TicketKind.PAID, 3, price=1000)
            holders = []
            for i in range(3):
                holders.append(svc.book(Actor.dispatcher("d"), "s1", "t1", 1,
                                        visitor_name=f"h{i}", idem_key=f"h{i}")["reservation_id"])
            waiter = svc.book(Actor.dispatcher("d"), "s1", "t1", 1,
                              visitor_name="候补", idem_key="w1")
            self.assertEqual(waiter["status"], "waitlisted")

            # 直接在库里制造一笔"已排队但服务在处理前崩溃"的退款
            with svc.store.write() as c:
                c.execute(
                    """INSERT INTO refunds(refund_id, reservation_id, reason, amount,
                           status, rule, idem_key, requested_at)
                       VALUES('rf_crash', ?, 'voluntary:crash', 1000, 'pending',
                              'S09', 'crash-1', '2026-09-29T20:00:00+00:00')""",
                    (holders[0],))
            del svc

            # 新进程打开同一个库并恢复
            svc2 = CapacityService(Store(path))
            out = svc2.resume_pending()
            self.assertIn("rf_crash", out["refunds_processed"])
            self.assertEqual(out["waitlist_promoted"].get("s1"),
                             [waiter["reservation_id"]])
            with svc2.store.read() as c:
                used = c.execute(
                    "SELECT COALESCE(SUM(qty),0) n FROM reservations "
                    "WHERE session_id='s1' AND status='confirmed'"
                ).fetchone()["n"]
                waiter_state = c.execute(
                    "SELECT status FROM reservations WHERE idem_key='w1'"
                ).fetchone()["status"]
                double = c.execute(
                    "SELECT COUNT(*) n FROM refunds WHERE reservation_id=? AND status='done'",
                    (holders[0],)).fetchone()["n"]
            self.assertEqual(used, 3)
            self.assertEqual(waiter_state, "confirmed")
            self.assertEqual(double, 1)  # 恢复不重复退款
            # 再恢复一次：幂等，没有新处理
            out2 = svc2.resume_pending()
            self.assertEqual(out2["refunds_processed"], [])
            self.assertEqual(out2["waitlist_promoted"], {})
        finally:
            os.unlink(path)
            for suffix in ("-wal", "-shm"):
                p = path + suffix
                if os.path.exists(p):
                    os.unlink(p)

    # ---- 时间线与解释视图 -----------------------------------------------
    def test_explain_session_tells_why_and_final_arrangement(self):
        tx = self.svc.book(self.cits, "s_a", "tt_a", 12, visitor_name="王领队",
                           team_id="team_x", idem_key="ex1",
                           preference={"accept_reroll": True})
        self.svc.cancel_session(self.dispatcher, "s_a", "设备故障")
        self.svc.resume_pending()

        report = self.svc.explain_session("s_a")
        self.assertEqual(report["session"]["state"], "cancelled")
        rules_fired = {r["rule"] for r in report["rules_triggered"]}
        self.assertIn("S11", rules_fired)
        self.assertIn("S13", rules_fired)

        story = next(x for x in report["seat_allocation"]
                     if x["reservation_id"] == tx["reservation_id"])
        self.assertEqual(story["status"], "rerolled")
        self.assertEqual(story["final_arrangement"]["reroll_to"], "s_b")
        self.assertIn("转场", story["why"])
        # 时间线上两种场次都留有同一条转场事件
        tl_a = self.svc.timeline("s_a")
        self.assertTrue(any(e["kind"] == "reroll" for e in tl_a))
        self.assertTrue(any(e["kind"] == "cancel" for e in tl_a))


if __name__ == "__main__":
    unittest.main()
