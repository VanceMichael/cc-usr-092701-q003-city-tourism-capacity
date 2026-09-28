"""端到端情景演示：中秋假期容量调度的一天。

运行：
    python3 -m src.city_tourism_capacity.demo

场景覆盖：多承办方排期、临时加场与限流、入境团队整团预约、
免费名额授权、并发不超卖、退款释放→候补 FIFO 顶位、跨区转场授权、
前一天场次冻结与迟到回执、"服务重启"后挂起退款/候补继续处理、
统一时间线与场次解释视图。
"""

from __future__ import annotations

import os
import tempfile
import textwrap

from .models import Actor, TicketKind
from .rules import PolicyError
from .service import CapacityService
from .store import Store


def hr(title: str) -> None:
    print("\n" + "=" * 72)
    print(title)
    print("=" * 72)


def show_rules(report: dict) -> None:
    if report["rules_triggered"]:
        print("  触发的规则：")
        for r in report["rules_triggered"]:
            print(f"    - {r['rule']}：{r['text']}")


def main() -> None:
    tmp = tempfile.NamedTemporaryFile(suffix=".db", delete=False)
    tmp.close()
    db_path = tmp.name
    print(f"数据库文件：{db_path}（演示结束后保留，可用 explain 复查）")

    svc = CapacityService(Store(db_path))

    dispatcher = Actor.dispatcher("值班调度员-林")
    cits = Actor.agency("旅行社操作号-国旅", "CITS")
    inbound = Actor.agency("旅行社操作号-入境中心", "INBOUND")

    hr("1. 排期：演出 / 游园 / 古建活动 / 京郊节庆 在同一张日程上（不同承办方）")
    svc.create_venue("v_jingshan", "景山公园", "西城区")
    svc.create_venue("v_huairou", "雁栖湖广场", "怀柔区")
    svc.create_session("s_show", "v_jingshan", "中秋国风夜演", "演出",
                       "2026-09-30T19:30", "2026-09-30T21:30", 200, actor=dispatcher)
    svc.add_ticket_type("tt_show", "s_show", "观演票", TicketKind.PAID, 200, price=12800)
    svc.add_ticket_type("tt_show_free", "s_show", "入境团队免费名额", TicketKind.FREE, 0)
    svc.create_session("s_temple", "v_jingshan", "古建赏月雅集", "古建活动",
                       "2026-09-30T18:00", "2026-09-30T20:00", 40, actor=dispatcher)
    svc.add_ticket_type("tt_temple", "s_temple", "雅集票", TicketKind.PAID, 40, price=6000)
    svc.create_session("s_harvest", "v_huairou", "京郊丰收节", "京郊节庆",
                       "2026-10-01T10:00", "2026-10-01T16:00", 500, actor=dispatcher)
    svc.add_ticket_type("tt_harvest", "s_harvest", "入场票", TicketKind.PAID, 500, price=3000)
    # 同区备选（转场目标）
    svc.create_session("s_show2", "v_jingshan", "国庆国风夜演", "演出",
                       "2026-10-02T19:30", "2026-10-02T21:30", 200, actor=dispatcher)
    svc.add_ticket_type("tt_show2", "s_show2", "观演票", TicketKind.PAID, 200, price=12800)
    print("  已排期：中秋国风夜演(200) / 古建赏月雅集(40) / 京郊丰收节(500) / 国庆夜演(200)")

    hr("2. 入境团队整团预约：35 人必须同进同退；免费名额先授权")
    svc.register_team("team_in", "INBOUND", "Tan 领队", 35, "tan@inbound.example")
    try:
        svc.book(inbound, "s_show", "tt_show_free", 35, visitor_name="Tan 领队",
                 team_id="team_in", idem_key="inbound-1",
                 preference={"accept_reroll": True, "same_activity_type": True})
    except PolicyError as e:
        print(f"  未授权签免费名额 → 拒绝：{e}")
    svc.grant_authorization(dispatcher, "旅行社操作号-入境中心", "free_quota",
                             "s_show", {"purpose": "入境团队文化交流", "qty": 40})
    with svc.store.write() as c:
        c.execute("UPDATE ticket_types SET total_qty=40 WHERE type_id='tt_show_free'")
    r_in = svc.book(inbound, "s_show", "tt_show_free", 35, visitor_name="Tan 领队",
                    team_id="team_in", idem_key="inbound-1",
                    preference={"accept_reroll": True, "same_activity_type": True})
    print(f"  授权后整团确认：状态={r_in['status']}，人数={r_in['qty']}")

    hr("3. 售票推进到满场，后续团队整团候补（不拆团、不超卖）")
    svc.register_team("team_cits", "CITS", "赵领队", 12, "13800000008")
    # 再售 165 张把 200 席占满（35 + 165 = 200）
    for i in range(165):
        svc.book(dispatcher, "s_show", "tt_show", 1,
                 visitor_name=f"散客{i:03d}", idem_key=f"retail-{i}")
    r_wait = svc.book(cits, "s_show", "tt_show", 12, visitor_name="赵领队",
                      team_id="team_cits", idem_key="cits-12")
    print(f"  夜演已确认 200 人；12 人国旅团队 → {r_wait['status']}（整团等待）")

    hr("4. 临时限流：安全容量 200 → 180（安保观察到疏散通道收窄）")
    cut = svc.adjust_safe_capacity(dispatcher, "s_show", 180, "疏散通道临时收窄")
    print(f"  挤出最晚确认的 {len(cut['displaced'])} 人；其中转场 "
          f"{len(cut['rerolled'])} 人，排队退款 {len(cut['refunds_queued'])} 笔")
    print("  （挤出的散客无转场偏好 → 全额退款入 outbox；候补 12 人团仍 > 剩余空间）")

    hr("5. 模拟服务短暂中断：退款挂起，重启新进程后 resume_pending()")
    del svc
    svc = CapacityService(Store(db_path))  # 新进程打开同一数据库
    resumed = svc.resume_pending()
    print(f"  恢复完成：处理挂起退款 {len(resumed['refunds_processed'])} 笔，"
          f"候补提升 {sum(len(v) for v in resumed['waitlist_promoted'].values())} 条")

    hr("6. 再有 12 名散客自愿退款 → 名额达到 12 → 候补整团 FIFO 自动顶位")
    with svc.store.read() as c:
        retail = [r["reservation_id"] for r in c.execute(
            "SELECT reservation_id FROM reservations WHERE team_id IS NULL "
            "AND status='confirmed' ORDER BY confirmed_at LIMIT 12")]
    for i, rid in enumerate(retail):
        svc.request_refund(dispatcher, rid, "行程冲突", f"vol-{i}")
    with svc.store.read() as c:
        state = c.execute(
            "SELECT status FROM reservations WHERE team_id='team_cits'"
        ).fetchone()["status"]
        used = c.execute(
            "SELECT COALESCE(SUM(qty),0) n FROM reservations "
            "WHERE session_id='s_show' AND status='confirmed'"
        ).fetchone()["n"]
    print(f"  国旅 12 人团：{state}；当前确认合计 {used}（安全容量 180，未超限）")

    hr("7. 权限隔离：旅行社互不可改、景区工作人员只碰本场名单")
    other_agency = Actor.agency("旅行社操作号-康辉", "KANGHUI")
    try:
        svc.request_refund(other_agency, r_in["reservation_id"], "恶意退团", "atk-1")
    except PolicyError as e:
        print(f"  康辉试图退国旅/入境中心的团 → {e.rule} 拒绝")
    staff_temple = Actor.site_staff("景山工作人员-周", "s_temple")
    try:
        svc.session_roster(staff_temple, "s_show")
    except PolicyError as e:
        print(f"  雅集场次岗位查夜演名单 → {e.rule} 拒绝")
    staff_show = Actor.site_staff("景山工作人员-吴", "s_show")
    roster = svc.session_roster(staff_show, "s_show")
    print(f"  夜演本场岗位可见名单 {len(roster)} 条（只含本场）")

    hr("8. 跨区域转场需要授权：丰收节怀柔 ← 夜演西城")
    try:
        svc.reroll_reservation(cits, r_wait["reservation_id"], "s_harvest")
    except PolicyError as e:
        print(f"  旅行社自行跨区 → {e.rule} 拒绝")
    svc.grant_authorization(dispatcher, "旅行社操作号-国旅",
                             "cross_region_reroll", "s_harvest",
                             {"reason": "假期跨区联动调配"})
    new_id = svc.reroll_reservation(cits, r_wait["reservation_id"], "s_harvest")
    print(f"  授权后 12 人团原子转入丰收节：新预约 {new_id}，原预约状态 rerolled")

    hr("9. 前一天场次结束即冻结：迟到退款回执不能重开")
    r_late = svc.book(dispatcher, "s_temple", "tt_temple", 2,
                      visitor_name="迟到渠道游客", idem_key="late-buy")
    svc.check_in(staff_temple, r_late["reservation_id"])
    svc.finish_session(dispatcher, "s_temple")
    print("  雅集场次正常结束（已核验 2 人），次日渠道退款回执才到达")
    ack = svc.request_refund(dispatcher, r_late["reservation_id"],
                             "渠道隔日迟到回执", "late-ack-1")
    print(f"  隔日回执 → 退款单状态 {ack['status']}，场次保持 finished，不重开")

    hr("10. 统一时间线：夜演的安全调整/候补/退款/转场/授权节选")
    focus = {"capacity_adjust", "reroll", "auth_grant", "waitlist",
             "rejected_ack", "compensation"}
    shown = 0
    for e in svc.timeline("s_show"):
        if e["kind"] not in focus:
            continue
        if shown >= 14:
            print("  …（其余售票/退款事件省略，完整时间线可通过 timeline() 导出）")
            break
        rule = f"[{e['rule']}]" if e["rule"] else "      "
        print(f"  {e['ts'][11:19]} {e['kind']:<15} {rule} {e['summary']}")
        shown += 1

    hr("11. 运营解释视图 explain_session('s_show') 摘要")
    report = svc.explain_session("s_show")
    s = report["session"]
    print(f"  {s['title']}：状态={s['state']}，安全容量={s['safe_capacity']}，"
          f"已确认={s['confirmed_seats']}，空余={s['free_seats']}")
    show_rules(report)
    interesting = [x for x in report["seat_allocation"]
                   if x["team_id"] in ("team_in", "team_cits")][:2]
    for x in interesting:
        print("  ---")
        print(f"  预约 {x['reservation_id']}：{x['visitor_name']} {x['qty']} 人"
              f"（团队 {x['team_id']}）→ 状态 {x['status']}")
        print(f"    为何这样分配：{x['why']}")
        if x["limiting_rule"]:
            print(f"    限制规则：{x['limiting_rule']}")
        fa = x["final_arrangement"]
        print(f"    最终安排：转场={fa['reroll_to']}，退款={fa['refund'] and fa['refund']['status']}，"
              f"补偿={fa['compensation'] and fa['compensation']['kind']}，已入场={fa['checked_in']}")

    print(textwrap.dedent(f"""
        ----------------------------------------------------------------
        演示数据保留在：{db_path}
        可自行连接复查，例如：
          python3 -c "from src.city_tourism_capacity.store import Store; \\
from src.city_tourism_capacity.service import CapacityService; \\
print(len(CapacityService(Store('{os.path.basename(db_path)}')).timeline('s_show')), '条时间线')"
    """))


if __name__ == "__main__":
    main()
