"""容量调度核心服务。

所有写操作都在 :class:`~city_tourism_capacity.storage.Storage` 的串行化
事务中完成，事务内遵循统一顺序：**先读容量 → 再写状态 → 追加决策/时间线**，
因此并发售票、退款不会出现重复占座或释放名额被重复卖出。

关键不变量：

1. 一个团队（一次多座预约）作为整体分配或整体候补，不会部分越过安全上限。
2. 场次状态非 ``scheduled``（已取消/已结束）后，迟到的支付回执、核验请求
   一律拒绝，不会重新开放名额。
3. 退款与候补全程落库（``pending``/``waiting``），服务重启后
   :meth:`Service.recover` 继续处理，不依赖内存队列。
4. 每次安全调整、取消、转场、补偿都追加到同一条场次时间线，每个名额的
   去向写入 ``allocation_decisions``，供运营逐名额解释。
"""

from __future__ import annotations

import json
import uuid
from collections.abc import Callable
from datetime import datetime, timezone

from .errors import (
    CapacityError,
    ConflictError,
    NotFoundError,
    PermissionDeniedError,
    SessionClosedError,
    TeamAtomicError,
    ValidationError,
)
from .security import (
    DISPATCHER,
    P_ACTIVITY_MANAGE,
    P_CHECKIN,
    P_COMPENSATION_DECIDE,
    P_CROSS_REGION,
    P_FREE_QUOTA,
    P_REFUND,
    P_ROSTER_READ,
    P_SAFETY_ADJUST,
    P_SELL,
    P_SESSION_CANCEL,
    P_SESSION_TRANSFER,
    P_TIMELINE_READ,
    P_VENUE_MANAGE,
    Actor,
)
from .storage import Storage

# 占用安全容量的预约状态
HELD_STATUSES = ("pending", "confirmed")
# 终态
TERMINAL_STATUSES = ("refunded", "cancelled", "transferred", "compensated")


def _uid(prefix: str) -> str:
    return f"{prefix}_{uuid.uuid4().hex[:16]}"


def _utcnow() -> str:
    return datetime.now(timezone.utc).isoformat()


# 规则码：解释“哪条规则触发了限制”
RULE = {
    "SESSION_OPEN": "session_open",
    "CAPACITY_FULL": "capacity_session_full",
    "TICKET_QUOTA_FULL": "ticket_quota_full",
    "TEAM_ATOMIC": "team_atomic_hold",
    "WAITLISTED": "waitlisted",
    "WAITLIST_PROMOTED": "waitlist_promoted",
    "WAITLIST_SKIPPED": "waitlist_skip_no_room_for_whole_team",
    "SEATS_RELEASED": "seats_released",
    "SAFETY_CAP": "safety_capacity_reduced",
    "SESSION_CANCELLED": "session_cancelled",
    "TRANSFER": "transfer_arranged",
    "REFUND": "refund_arranged",
    "COMPENSATION": "compensation_decided",
    "FREE_GRANT": "free_quota_grant_consumed",
    "LATE_RECEIPT": "late_receipt_rejected_session_closed",
    "CHECKIN_DENIED": "checkin_denied_session_closed",
    "SESSION_ENDED": "session_ended_pending_auto_cancelled",
}


class Service:
    def __init__(
        self,
        storage: Storage,
        now: Callable[[], datetime] | None = None,
        payment_gateway: Callable[[str, int], bool] | None = None,
    ) -> None:
        self.db = storage
        self._now_fn = now or (lambda: datetime.now(timezone.utc))
        # 支付网关：返回 True 表示受理成功；默认成功。可注入失败以模拟中断。
        self._gateway = payment_gateway or (lambda refund_id, amount: True)

    # ------------------------------------------------------------------ #
    # 基础工具
    # ------------------------------------------------------------------ #

    def _now(self) -> datetime:
        return self._now_fn()

    def _ts(self) -> str:
        return self._now().isoformat()

    def _row(self, cur, sql: str, params=()):
        cur.execute(sql, params)
        row = cur.fetchone()
        if row is None:
            raise NotFoundError(f"对象不存在：{sql} {params}")
        return row

    def _timeline(self, cur, session_id, actor: Actor, etype: str,
                  summary: str, payload: dict | None = None) -> None:
        cur.execute(
            "INSERT INTO timeline_events VALUES (?,?,?,?,?,?,?,?)",
            (
                _uid("evt"), session_id, self._ts(), actor.role, actor.name,
                etype, summary, json.dumps(payload or {}, ensure_ascii=False),
            ),
        )

    def _decision(self, cur, session_id, decision: str, seats: int,
                  rule_code: str, reason: str, arrangement: str,
                  booking_id: str | None = None, team_id: str | None = None,
                  payload: dict | None = None) -> None:
        cur.execute(
            "INSERT INTO allocation_decisions VALUES (?,?,?,?,?,?,?,?,?,?,?)",
            (
                _uid("dec"), session_id, booking_id, team_id, self._ts(),
                decision, seats, rule_code, reason, arrangement,
                json.dumps(payload or {}, ensure_ascii=False),
            ),
        )

    def _close_expired(self, cur) -> None:
        """把结束时间已过、且仍是 scheduled 的场次标记为 finished。

        结束即终态：之后任何迟到回执都不能使其复活。结束时仍处于
        ``pending``（支付回执未到）的占座自动取消并退款，名额不再售卖。
        """
        cur.execute(
            "SELECT session_id FROM sessions "
            "WHERE status='scheduled' AND end_ts <= ?",
            (self._ts(),),
        )
        expired = [r["session_id"] for r in cur.fetchall()]
        system = Actor(DISPATCHER, "system")
        for session_id in expired:
            cur.execute(
                "SELECT * FROM bookings WHERE session_id=? AND status='pending'",
                (session_id,),
            )
            for bk in cur.fetchall():
                cur.execute(
                    "UPDATE bookings SET status='cancelled',"
                    "version=version+1,updated_ts=? WHERE booking_id=?",
                    (self._ts(), bk["booking_id"]),
                )
                refund_id = self._create_refund(cur, bk, "场次已结束，支付回执未到达")
                self._decision(
                    cur, session_id, "rejected", bk["seats"],
                    RULE["SESSION_ENDED"],
                    "场次结束时支付仍未完成，占座自动取消",
                    f"refund:{refund_id}",
                    booking_id=bk["booking_id"], team_id=bk["team_id"],
                )
            # 结束后候补队列一并关闭，不再可能递补
            cur.execute(
                "SELECT * FROM bookings WHERE session_id=? AND status='waiting'",
                (session_id,),
            )
            for bk in cur.fetchall():
                cur.execute(
                    "UPDATE bookings SET status='cancelled',"
                    "version=version+1,updated_ts=? WHERE booking_id=?",
                    (self._ts(), bk["booking_id"]),
                )
                self._decision(
                    cur, session_id, "rejected", bk["seats"],
                    RULE["SESSION_ENDED"],
                    "场次结束，候补队列关闭",
                    "cancelled",
                    booking_id=bk["booking_id"], team_id=bk["team_id"],
                )
            cur.execute(
                "UPDATE sessions SET status='finished', version=version+1 "
                "WHERE session_id=?",
                (session_id,),
            )
            self._timeline(
                cur, session_id, system, "session_finished",
                "场次结束时间已到，自动关闭；未支付占座已取消", {},
            )

    def _finalize_expired(self) -> None:
        """在独立事务中完成到期收尾并立即提交。

        收尾（场次置 finished、未支付占座取消退款）绝不能因随后业务事务
        回滚（例如拒绝一个迟到回执）而被撤销，否则结束的场次会一直停留在
        scheduled 状态、被下一个请求反复“重开”。
        """
        with self.db.tx() as cur:
            self._close_expired(cur)

    def _get_open_session(self, cur, session_id: str):
        row = self._row(
            cur, "SELECT * FROM sessions WHERE session_id=?", (session_id,)
        )
        if row["status"] != "scheduled" or row["end_ts"] <= self._ts():
            raise SessionClosedError(
                f"场次 {session_id} 当前状态为 {row['status']}，不再受理变更"
            )
        return row

    def _held(self, cur, session_id: str) -> int:
        cur.execute(
            "SELECT COALESCE(SUM(seats),0) AS n FROM bookings "
            "WHERE session_id=? AND status IN ('pending','confirmed')",
            (session_id,),
        )
        return cur.fetchone()["n"]

    def _held_ticket(self, cur, ticket_type_id: str) -> int:
        cur.execute(
            "SELECT COALESCE(SUM(seats),0) AS n FROM bookings "
            "WHERE ticket_type_id=? AND status IN ('pending','confirmed')",
            (ticket_type_id,),
        )
        return cur.fetchone()["n"]

    # ------------------------------------------------------------------ #
    # 基础资料维护（演示/建单用）
    # ------------------------------------------------------------------ #

    def create_venue(self, actor: Actor, venue_id: str, name: str,
                     region: str) -> dict:
        actor.require(P_VENUE_MANAGE)
        with self.db.tx() as cur:
            cur.execute(
                "INSERT INTO venues VALUES (?,?,?,?)",
                (venue_id, name, region, self._ts()),
            )
        return {"venue_id": venue_id}

    def create_activity(self, actor: Actor, activity_id: str, title: str,
                        kind: str, organizer: str) -> dict:
        actor.require(P_ACTIVITY_MANAGE)
        with self.db.tx() as cur:
            cur.execute(
                "INSERT INTO activities VALUES (?,?,?,?,?)",
                (activity_id, title, kind, organizer, self._ts()),
            )
        return {"activity_id": activity_id}

    def create_session(self, actor: Actor, session_id: str, activity_id: str,
                       venue_id: str, start_ts: str, end_ts: str,
                       capacity: int) -> dict:
        actor.require(P_ACTIVITY_MANAGE)
        if capacity < 0:
            raise ValidationError("容量不能为负")
        if end_ts <= start_ts:
            raise ValidationError("结束时间必须晚于开始时间")
        with self.db.tx() as cur:
            self._row(cur, "SELECT 1 FROM activities WHERE activity_id=?",
                      (activity_id,))
            self._row(cur, "SELECT 1 FROM venues WHERE venue_id=?", (venue_id,))
            cur.execute(
                "INSERT INTO sessions (session_id,activity_id,venue_id,"
                "start_ts,end_ts,status,base_capacity,safety_capacity,version)"
                " VALUES (?,?,?,?,?,'scheduled',?,?,0)",
                (session_id, activity_id, venue_id, start_ts, end_ts,
                 capacity, capacity),
            )
            self._timeline(cur, session_id, actor, "session_created",
                           f"场次建立，安全容量 {capacity} 人",
                           {"capacity": capacity})
        return {"session_id": session_id}

    def create_ticket_type(self, actor: Actor, ticket_type_id: str,
                           session_id: str, name: str, price: int,
                           quota: int | None = None, is_free: bool = False,
                           requires_auth: bool = False) -> dict:
        with self.db.tx() as cur:
            self._row(cur, "SELECT 1 FROM sessions WHERE session_id=?",
                      (session_id,))
            cur.execute(
                "INSERT INTO ticket_types VALUES (?,?,?,?,?,?,?)",
                (ticket_type_id, session_id, name, price, quota,
                 1 if is_free else 0, 1 if requires_auth else 0),
            )
        return {"ticket_type_id": ticket_type_id}

    def create_team(self, actor: Actor, team_id: str, name: str,
                    contact: str | None = None,
                    inbound: bool = False) -> dict:
        """旅行社登记团队；游客服务人员可登记散客（agency_id 为空）。"""
        if actor.role not in ("dispatcher", "agency", "visitor_service"):
            raise PermissionDeniedError("该角色不能登记团队")
        agency_id: str | None = None
        if actor.role == "agency":
            agency_id = actor.agency_id
        with self.db.tx() as cur:
            cur.execute(
                "INSERT INTO teams VALUES (?,?,?,?,?,?)",
                (team_id, agency_id, name, 1 if inbound else 0, contact,
                 self._ts()),
            )
        return {"team_id": team_id}

    # ------------------------------------------------------------------ #
    # 售票（团队整体、容量原子）
    # ------------------------------------------------------------------ #

    def sell(self, actor: Actor, session_id: str, ticket_type_id: str,
             seats: int, team_id: str | None = None,
             allow_waitlist: bool = True, pending_payment: bool = False) -> dict:
        """一次售票就是一个不可分割的团队单元（seats 张必须同时满足）。"""
        actor.require(P_SELL)
        if seats <= 0:
            raise ValidationError("预约人数必须为正数")
        self._finalize_expired()
        with self.db.tx() as cur:
            session = self._get_open_session(cur, session_id)
            tt = self._row(
                cur, "SELECT * FROM ticket_types WHERE ticket_type_id=?",
                (ticket_type_id,),
            )
            if tt["session_id"] != session_id:
                raise ValidationError("票种不属于该场次")

            if team_id is None:
                team_id = _uid("team_walkin")
                walkin_agency = actor.agency_id if actor.role == "agency" else None
                cur.execute(
                    "INSERT INTO teams VALUES (?,?,?,?,?,?)",
                    (team_id, walkin_agency, "散客", 0, None, self._ts()),
                )
            else:
                team = self._row(
                    cur, "SELECT * FROM teams WHERE team_id=?", (team_id,)
                )
                actor.require_agency(team["agency_id"])

            held = self._held(cur, session_id)
            held_tt = self._held_ticket(cur, ticket_type_id)
            room = session["safety_capacity"] - held
            tt_room = None
            if tt["quota"] is not None:
                tt_room = tt["quota"] - held_tt

            grant_row = None
            if tt["requires_auth"]:
                # 免费/授权票种：必须有覆盖该团队或场次通用的授权额度；
                # 优先消耗团队专属额度，再使用场次通用额度。
                cur.execute(
                    "SELECT * FROM free_grants WHERE session_id=? "
                    "AND ticket_type_id=? AND (team_id IS ? OR team_id=?) "
                    "AND seats-used >= ? "
                    "ORDER BY CASE WHEN team_id=? THEN 0 ELSE 1 END, created_ts",
                    (session_id, ticket_type_id, None, team_id, seats, team_id),
                )
                grant_row = cur.fetchone()
                if grant_row is None:
                    raise PermissionDeniedError(
                        "该票种需授权额度（免费名额/跨区域调配），未找到可用授权"
                    )

            fits = room >= seats and (tt_room is None or tt_room >= seats)
            booking_id = _uid("bk")
            status = "pending" if pending_payment else "confirmed"
            total = tt["price"] * seats

            if fits:
                cur.execute(
                    "INSERT INTO bookings (booking_id,session_id,team_id,"
                    "ticket_type_id,seats,status,price_each,total_amount,"
                    "compensation,original_booking_id,created_ts,updated_ts,"
                    "version) VALUES (?,?,?,?,?,?,?,?,0,NULL,?,?,0)",
                    (booking_id, session_id, team_id, ticket_type_id, seats,
                     status, tt["price"], total, self._ts(), self._ts()),
                )
                if grant_row is not None:
                    cur.execute(
                        "UPDATE free_grants SET used=used+? WHERE grant_id=?",
                        (seats, grant_row["grant_id"]),
                    )
                self._decision(
                    cur, session_id, "allocated", seats,
                    RULE["SESSION_OPEN"],
                    f"场次剩余 {room} 席，团队 {seats} 人整体占座成功",
                    status,
                    booking_id=booking_id, team_id=team_id,
                    payload={"held_after": held + seats,
                             "safety_capacity": session["safety_capacity"],
                             "rule": RULE["FREE_GRANT"] if grant_row else None},
                )
                return {"booking_id": booking_id, "status": status,
                        "seats": seats}

            # 容量不足：团队不得拆分，整体进入候补（或直接拒绝）
            reason_parts = [f"场次剩余 {room} 席，无法整体容纳 {seats} 人"]
            rule = RULE["TEAM_ATOMIC"] if seats > 1 else RULE["CAPACITY_FULL"]
            if tt_room is not None and tt_room < seats:
                rule = RULE["TICKET_QUOTA_FULL"]
                reason_parts.append(f"票种剩余 {tt_room} 席")
            if not allow_waitlist:
                raise TeamAtomicError(
                    "；".join(reason_parts) + "，且未允许候补"
                )
            cur.execute(
                "INSERT INTO bookings (booking_id,session_id,team_id,"
                "ticket_type_id,seats,status,price_each,total_amount,"
                "compensation,original_booking_id,created_ts,updated_ts,"
                "version) VALUES (?,?,?,?,?,'waiting',?,?,0,NULL,?,?,0)",
                (booking_id, session_id, team_id, ticket_type_id, seats,
                 tt["price"], total, self._ts(), self._ts()),
            )
            self._decision(
                cur, session_id, "waitlisted", seats, rule,
                "；".join(reason_parts) + "，团队整体进入候补队列",
                "waiting",
                booking_id=booking_id, team_id=team_id,
                payload={"room": room, "ticket_room": tt_room},
            )
            return {"booking_id": booking_id, "status": "waiting",
                    "seats": seats}

    def confirm_payment(self, actor: Actor, booking_id: str) -> dict:
        """支付渠道回执到达。场次已关闭则拒绝，绝不重开。"""
        self._finalize_expired()
        with self.db.read() as cur:
            bk = self._row(
                cur, "SELECT * FROM bookings WHERE booking_id=?", (booking_id,)
            )
            session = self._row(
                cur, "SELECT * FROM sessions WHERE session_id=?",
                (bk["session_id"],),
            )
            closed = session["status"] != "scheduled"
        if closed:
            # 拒绝审计在独立事务中落库，场次状态不被改动
            with self.db.tx() as cur:
                self._decision(
                    cur, bk["session_id"], "rejected", bk["seats"],
                    RULE["LATE_RECEIPT"],
                    f"场次状态 {session['status']}，迟到支付回执被拒绝",
                    "session_unchanged",
                    booking_id=booking_id, team_id=bk["team_id"],
                )
                self._timeline(
                    cur, bk["session_id"], actor, "late_receipt_rejected",
                    f"预约 {booking_id} 的迟到回执被拒绝，场次不重开",
                    {"booking_id": booking_id,
                     "session_status": session["status"]},
                )
            raise SessionClosedError(
                f"支付回执迟到，场次已 {session['status']}，不再受理"
            )
        if bk["status"] != "pending":
            raise ConflictError(
                f"预约当前状态 {bk['status']}，无需支付确认"
            )
        with self.db.tx() as cur:
            # 事务内复核，防止读与写之间场次被取消
            row = self._row(
                cur, "SELECT s.status AS sstatus, b.status AS bstatus "
                "FROM bookings b JOIN sessions s ON b.session_id=s.session_id "
                "WHERE b.booking_id=?",
                (booking_id,),
            )
            if row["sstatus"] != "scheduled":
                raise SessionClosedError("支付确认时场次已关闭")
            if row["bstatus"] != "pending":
                raise ConflictError(
                    f"预约当前状态 {row['bstatus']}，无需支付确认"
                )
            cur.execute(
                "UPDATE bookings SET status='confirmed',version=version+1,"
                "updated_ts=? WHERE booking_id=?",
                (self._ts(), booking_id),
            )
            return {"booking_id": booking_id, "status": "confirmed"}

    # ------------------------------------------------------------------ #
    # 退款与候补递补
    # ------------------------------------------------------------------ #

    def _create_refund(self, cur, bk, reason: str) -> str:
        refund_id = _uid("rf")
        cur.execute(
            "INSERT INTO refunds (refund_id,booking_id,amount,reason,status,"
            "attempts,created_ts,finished_ts) VALUES (?,?,?,?,'pending',0,?,NULL)",
            (refund_id, bk["booking_id"], bk["total_amount"], reason,
             self._ts()),
        )
        return refund_id

    def _settle_refund(self, cur, refund_id: str) -> bool:
        """调用支付网关；失败保留 pending，等待恢复重试。"""
        rf = self._row(
            cur, "SELECT * FROM refunds WHERE refund_id=?", (refund_id,)
        )
        if rf["status"] == "done":
            return True
        cur.execute(
            "UPDATE refunds SET attempts=attempts+1 WHERE refund_id=?",
            (refund_id,),
        )
        if self._gateway(refund_id, rf["amount"]):
            cur.execute(
                "UPDATE refunds SET status='done',finished_ts=? "
                "WHERE refund_id=?",
                (self._ts(), refund_id),
            )
            return True
        return False

    def refund(self, actor: Actor, booking_id: str, reason: str) -> dict:
        actor.require(P_REFUND)
        with self.db.tx() as cur:
            bk = self._row(
                cur, "SELECT * FROM bookings WHERE booking_id=?", (booking_id,)
            )
            if bk["team_id"]:
                team = self._row(
                    cur, "SELECT * FROM teams WHERE team_id=?", (bk["team_id"],)
                )
                actor.require_agency(team["agency_id"])
            if bk["status"] not in ("confirmed", "pending", "affected"):
                raise ConflictError(
                    f"预约状态 {bk['status']} 不可退款"
                )
            refund_id = self._create_refund(cur, bk, reason)
            cur.execute(
                "UPDATE bookings SET status='refunded',version=version+1,"
                "updated_ts=? WHERE booking_id=?",
                (self._ts(), booking_id),
            )
            settled = self._settle_refund(cur, refund_id)
            self._decision(
                cur, bk["session_id"], "released", bk["seats"],
                RULE["REFUND"], f"退款释放名额：{reason}",
                f"refund:{refund_id}:{'done' if settled else 'pending'}",
                booking_id=booking_id, team_id=bk["team_id"],
            )
            self._timeline(
                cur, bk["session_id"], actor, "refund",
                f"预约 {booking_id} 退款 {bk['seats']} 席，释放回可用容量",
                {"booking_id": booking_id, "refund_id": refund_id,
                 "gateway": "done" if settled else "pending"},
            )
            # 同一事务内立即按候补顺序递补
            promoted = self._promote_waitlist(cur, bk["session_id"])
            return {"refund_id": refund_id,
                    "status": "done" if settled else "pending",
                    "promoted": promoted}

    def _promote_waitlist(self, cur, session_id: str) -> list[dict]:
        """按候补时间顺序递补；团队必须整体放得下，放不下则跳过并记录。

        释放出的名额在本事务内完成递补，其它请求只能看到递补后的容量，
        因此同一批名额不可能被重复卖出。
        """
        promoted: list[dict] = []
        cur.execute(
            "SELECT * FROM bookings WHERE status='waiting' AND session_id=? "
            "ORDER BY created_ts, rowid",
            (session_id,),
        )
        waiting = cur.fetchall()
        if not waiting:
            return promoted
        session = self._row(
            cur, "SELECT * FROM sessions WHERE session_id=?", (session_id,)
        )
        if session["status"] != "scheduled":
            # 已结束/已取消的场次，候补不再递补
            return promoted
        for bk in waiting:
            held = self._held(cur, session_id)
            room = session["safety_capacity"] - held
            tt = self._row(
                cur, "SELECT * FROM ticket_types WHERE ticket_type_id=?",
                (bk["ticket_type_id"],),
            )
            held_tt = self._held_ticket(cur, bk["ticket_type_id"])
            tt_room = (tt["quota"] - held_tt) if tt["quota"] is not None else None
            if room < bk["seats"]:
                self._decision(
                    cur, session_id, "rejected", bk["seats"],
                    RULE["WAITLIST_SKIPPED"],
                    f"候补团队需 {bk['seats']} 席，仅剩 {room} 席，整体跳过",
                    "waiting",
                    booking_id=bk["booking_id"], team_id=bk["team_id"],
                )
                continue
            if tt_room is not None and tt_room < bk["seats"]:
                self._decision(
                    cur, session_id, "rejected", bk["seats"],
                    RULE["TICKET_QUOTA_FULL"],
                    f"票种额度仅剩 {tt_room} 席，候补团队整体跳过",
                    "waiting",
                    booking_id=bk["booking_id"], team_id=bk["team_id"],
                )
                continue
            if tt["requires_auth"]:
                cur.execute(
                    "SELECT * FROM free_grants WHERE session_id=? "
                    "AND ticket_type_id=? AND (team_id IS ? OR team_id=?) "
                    "AND seats-used >= ? "
                    "ORDER BY CASE WHEN team_id=? THEN 0 ELSE 1 END, created_ts",
                    (session_id, bk["ticket_type_id"], None, bk["team_id"],
                     bk["seats"], bk["team_id"]),
                )
                grant_row = cur.fetchone()
                if grant_row is None:
                    self._decision(
                        cur, session_id, "rejected", bk["seats"],
                        RULE["FREE_GRANT"],
                        "候补票种需授权额度且无可用授权，继续等待",
                        "waiting",
                        booking_id=bk["booking_id"], team_id=bk["team_id"],
                    )
                    continue
                cur.execute(
                    "UPDATE free_grants SET used=used+? WHERE grant_id=?",
                    (bk["seats"], grant_row["grant_id"]),
                )
            cur.execute(
                "UPDATE bookings SET status='confirmed',version=version+1,"
                "updated_ts=? WHERE booking_id=? AND status='waiting'",
                (self._ts(), bk["booking_id"]),
            )
            self._decision(
                cur, session_id, "promoted", bk["seats"],
                RULE["WAITLIST_PROMOTED"],
                f"释放名额后按候补顺序整体递补 {bk['seats']} 人",
                "confirmed",
                booking_id=bk["booking_id"], team_id=bk["team_id"],
                payload={"held_after": held + bk["seats"]},
            )
            promoted.append({"booking_id": bk["booking_id"],
                             "seats": bk["seats"]})
        return promoted

    # ------------------------------------------------------------------ #
    # 调度员：安全调整 / 取消 / 转场 / 补偿 / 免费授权
    # ------------------------------------------------------------------ #

    def adjust_safety_capacity(self, actor: Actor, session_id: str,
                               new_capacity: int, reason: str) -> dict:
        actor.require(P_SAFETY_ADJUST)
        if new_capacity < 0:
            raise ValidationError("安全容量不能为负")
        self._finalize_expired()
        with self.db.tx() as cur:
            session = self._get_open_session(cur, session_id)
            old = session["safety_capacity"]
            cur.execute(
                "UPDATE sessions SET safety_capacity=?,version=version+1 "
                "WHERE session_id=?",
                (new_capacity, session_id),
            )
            displaced: list[dict] = []
            if new_capacity < old:
                # 从最晚确认的预约开始整体移出，直到不超过新上限
                cur.execute(
                    "SELECT * FROM bookings WHERE session_id=? "
                    "AND status IN ('pending','confirmed') "
                    "ORDER BY created_ts DESC, rowid DESC",
                    (session_id,),
                )
                candidates = cur.fetchall()
                held = self._held(cur, session_id)
                for bk in candidates:
                    if held <= new_capacity:
                        break
                    cur.execute(
                        "UPDATE bookings SET status='affected',"
                        "version=version+1,updated_ts=? WHERE booking_id=?",
                        (self._ts(), bk["booking_id"]),
                    )
                    held -= bk["seats"]
                    displaced.append({"booking_id": bk["booking_id"],
                                      "seats": bk["seats"],
                                      "team_id": bk["team_id"]})
                    self._decision(
                        cur, session_id, "rejected", bk["seats"],
                        RULE["SAFETY_CAP"],
                        f"安全容量 {old}→{new_capacity}：{reason}，"
                        f"该团队整体移出，占用降至 {held}",
                        "affected_pending_compensation",
                        booking_id=bk["booking_id"], team_id=bk["team_id"],
                    )
            self._timeline(
                cur, session_id, actor, "safety_adjusted",
                f"安全容量由 {old} 调整为 {new_capacity}（{reason}），"
                f"{len(displaced)} 个团队受影响",
                {"old_capacity": old, "new_capacity": new_capacity,
                 "reason": reason, "displaced": displaced},
            )
            promoted = self._promote_waitlist(cur, session_id)
            return {"session_id": session_id, "safety_capacity": new_capacity,
                    "displaced": displaced, "promoted": promoted}

    def cancel_session(self, actor: Actor, session_id: str,
                       reason: str) -> dict:
        actor.require(P_SESSION_CANCEL)
        self._finalize_expired()
        with self.db.tx() as cur:
            session = self._row(
                cur, "SELECT * FROM sessions WHERE session_id=?", (session_id,)
            )
            if session["status"] == "cancelled":
                raise ConflictError("场次已取消")
            if session["status"] != "scheduled":
                raise SessionClosedError(
                    f"场次已 {session['status']}，不能取消"
                )
            cur.execute(
                "UPDATE sessions SET status='cancelled',version=version+1 "
                "WHERE session_id=?",
                (session_id,),
            )
            cur.execute(
                "SELECT * FROM bookings WHERE session_id=? AND status IN "
                "('pending','confirmed','affected','waiting')",
                (session_id,),
            )
            refunds: list[str] = []
            for bk in cur.fetchall():
                if bk["status"] in ("confirmed", "pending", "affected"):
                    refund_id = self._create_refund(cur, bk, f"场次取消：{reason}")
                    cur.execute(
                        "UPDATE bookings SET status='refunded',"
                        "version=version+1,updated_ts=? WHERE booking_id=?",
                        (self._ts(), bk["booking_id"]),
                    )
                    self._settle_refund(cur, refund_id)
                    refunds.append(refund_id)
                    self._decision(
                        cur, session_id, "released", bk["seats"],
                        RULE["SESSION_CANCELLED"],
                        f"场次取消：{reason}，票款全额退还",
                        f"refund:{refund_id}",
                        booking_id=bk["booking_id"], team_id=bk["team_id"],
                    )
                else:  # waiting
                    cur.execute(
                        "UPDATE bookings SET status='cancelled',"
                        "version=version+1,updated_ts=? WHERE booking_id=?",
                        (self._ts(), bk["booking_id"]),
                    )
                    self._decision(
                        cur, session_id, "rejected", bk["seats"],
                        RULE["SESSION_CANCELLED"],
                        f"场次取消：{reason}，候补自动关闭",
                        "cancelled",
                        booking_id=bk["booking_id"], team_id=bk["team_id"],
                    )
            self._timeline(
                cur, session_id, actor, "session_cancelled",
                f"场次取消：{reason}；{len(refunds)} 笔退款已安排",
                {"reason": reason, "refunds": refunds},
            )
            return {"session_id": session_id, "status": "cancelled",
                    "refunds": refunds}

    def grant_free_quota(self, actor: Actor, session_id: str,
                         ticket_type_id: str, seats: int,
                         team_id: str | None = None) -> dict:
        """免费名额/授权额度，必须经调度员授权。"""
        actor.require(P_FREE_QUOTA)
        with self.db.tx() as cur:
            self._row(cur, "SELECT 1 FROM sessions WHERE session_id=?",
                      (session_id,))
            tt = self._row(
                cur, "SELECT * FROM ticket_types WHERE ticket_type_id=?",
                (ticket_type_id,),
            )
            if tt["session_id"] != session_id:
                raise ValidationError("票种不属于该场次")
            if team_id is not None:
                self._row(
                    cur, "SELECT 1 FROM teams WHERE team_id=?", (team_id,)
                )
            grant_id = _uid("gr")
            cur.execute(
                "INSERT INTO free_grants VALUES (?,?,?,?,?,?,?,?)",
                (grant_id, session_id, ticket_type_id, team_id, seats, 0,
                 actor.name, self._ts()),
            )
            self._timeline(
                cur, session_id, actor, "free_quota_granted",
                f"授权免费名额 {seats} 席"
                + (f"（团队 {team_id}）" if team_id else "（场次通用）"),
                {"grant_id": grant_id, "seats": seats, "team_id": team_id},
            )
            promoted = self._promote_waitlist(cur, session_id)
            return {"grant_id": grant_id, "seats": seats, "promoted": promoted}

    def transfer(self, actor: Actor, booking_id: str,
                 target_session_id: str,
                 target_ticket_type_id: str | None = None) -> dict:
        """把一个团队预约整体转场；跨区域需额外授权。"""
        actor.require(P_SESSION_TRANSFER)
        self._finalize_expired()
        with self.db.tx() as cur:
            bk = self._row(
                cur, "SELECT * FROM bookings WHERE booking_id=?", (booking_id,)
            )
            team = self._row(
                cur, "SELECT * FROM teams WHERE team_id=?", (bk["team_id"],)
            )
            actor.require_agency(team["agency_id"])
            if bk["status"] not in ("confirmed", "affected"):
                raise ConflictError(
                    f"预约状态 {bk['status']}，仅已确认/受影响预约可转场"
                )
            src = self._row(
                cur, "SELECT * FROM sessions WHERE session_id=?",
                (bk["session_id"],),
            )
            dst = self._get_open_session(cur, target_session_id)
            src_venue = self._row(
                cur, "SELECT * FROM venues WHERE venue_id=?", (src["venue_id"],)
            )
            dst_venue = self._row(
                cur, "SELECT * FROM venues WHERE venue_id=?", (dst["venue_id"],)
            )
            if src_venue["region"] != dst_venue["region"] and not actor.can(
                P_CROSS_REGION
            ):
                raise PermissionDeniedError("跨区域调配需调度员授权")

            tt_id = target_ticket_type_id or bk["ticket_type_id"]
            tt = self._row(
                cur, "SELECT * FROM ticket_types WHERE ticket_type_id=?",
                (tt_id,),
            )
            if tt["session_id"] != target_session_id:
                raise ValidationError("目标票种不属于目标场次")
            room = dst["safety_capacity"] - self._held(cur, target_session_id)
            if room < bk["seats"]:
                raise TeamAtomicError(
                    f"目标场次仅剩 {room} 席，无法整体容纳 {bk['seats']} 人"
                )
            tt_room = None
            if tt["quota"] is not None:
                tt_room = tt["quota"] - self._held_ticket(cur, tt_id)
                if tt_room < bk["seats"]:
                    raise CapacityError("目标票种额度不足")

            new_id = _uid("bk")
            cur.execute(
                "INSERT INTO bookings (booking_id,session_id,team_id,"
                "ticket_type_id,seats,status,price_each,total_amount,"
                "compensation,original_booking_id,created_ts,updated_ts,"
                "version) VALUES (?,?,?,?,?,'confirmed',?,?,0,?,?,?,0)",
                (new_id, target_session_id, bk["team_id"], tt_id, bk["seats"],
                 tt["price"], tt["price"] * bk["seats"], bk["booking_id"],
                 self._ts(), self._ts()),
            )
            cur.execute(
                "UPDATE bookings SET status='transferred',version=version+1,"
                "updated_ts=? WHERE booking_id=?",
                (self._ts(), booking_id),
            )
            for sess_id, decision, rule, reason, arr, bid in (
                (bk["session_id"], "released", RULE["TRANSFER"],
                 f"团队整体转场至 {target_session_id}", f"transfer:{new_id}",
                 booking_id),
                (target_session_id, "allocated", RULE["TRANSFER"],
                 f"由 {bk['session_id']} 整体转入 {bk['seats']} 人",
                 "confirmed", new_id),
            ):
                self._decision(cur, sess_id, decision, bk["seats"], rule,
                               reason, arr, booking_id=bid,
                               team_id=bk["team_id"])
            self._timeline(
                cur, bk["session_id"], actor, "transferred_out",
                f"团队 {bk['team_id']}（{bk['seats']} 人）转出至 "
                f"{target_session_id}",
                {"booking_id": booking_id, "new_booking_id": new_id,
                 "target_session_id": target_session_id},
            )
            self._timeline(
                cur, target_session_id, actor, "transferred_in",
                f"团队 {bk['team_id']}（{bk['seats']} 人）由 "
                f"{bk['session_id']} 转入",
                {"booking_id": new_id, "source_session_id": bk["session_id"]},
            )
            if src["status"] == "scheduled":
                self._promote_waitlist(cur, bk["session_id"])
            return {"booking_id": new_id, "status": "confirmed",
                    "source_booking_id": booking_id}

    def decide_compensation(self, actor: Actor, booking_id: str,
                            arrangement: str, amount: int = 0,
                            target_session_id: str | None = None,
                            target_ticket_type_id: str | None = None) -> dict:
        """对受安全调整影响的游客给出最终安排：refund / transfer / voucher。"""
        actor.require(P_COMPENSATION_DECIDE)
        self._finalize_expired()
        if arrangement not in ("refund", "transfer", "voucher"):
            raise ValidationError("安排必须是 refund / transfer / voucher")
        with self.db.tx() as cur:
            bk = self._row(
                cur, "SELECT * FROM bookings WHERE booking_id=?", (booking_id,)
            )
            if bk["status"] != "affected":
                raise ConflictError(
                    f"预约状态 {bk['status']}，非待补偿状态"
                )
            if arrangement == "refund":
                refund_id = self._create_refund(
                    cur, bk, "安全容量调整补偿：退款"
                )
                cur.execute(
                    "UPDATE bookings SET status='refunded',compensation=?,"
                    "version=version+1,updated_ts=? WHERE booking_id=?",
                    (amount, self._ts(), booking_id),
                )
                settled = self._settle_refund(cur, refund_id)
                final = (
                    f"refund:{refund_id}+compensation:{amount}"
                    f":{'done' if settled else 'pending'}"
                )
                promoted = self._promote_waitlist(cur, bk["session_id"])
            elif arrangement == "voucher":
                cur.execute(
                    "UPDATE bookings SET status='compensated',compensation=?,"
                    "version=version+1,updated_ts=? WHERE booking_id=?",
                    (amount, self._ts(), booking_id),
                )
                final = f"voucher:{amount}"
            else:
                if not target_session_id:
                    raise ValidationError("转场补偿必须指定目标场次")
                dst = self._get_open_session(cur, target_session_id)
                room = dst["safety_capacity"] - self._held(
                    cur, target_session_id
                )
                if room < bk["seats"]:
                    raise TeamAtomicError("目标场次无法整体容纳该团队")
                tt_id = target_ticket_type_id or bk["ticket_type_id"]
                new_id = _uid("bk")
                cur.execute(
                    "INSERT INTO bookings (booking_id,session_id,team_id,"
                    "ticket_type_id,seats,status,price_each,total_amount,"
                    "compensation,original_booking_id,created_ts,updated_ts,"
                    "version) VALUES (?,?,?,?,?,'confirmed',?,?,?,?,?,?,0)",
                    (new_id, target_session_id, bk["team_id"], tt_id,
                     bk["seats"], bk["price_each"], bk["total_amount"], amount,
                     booking_id, self._ts(), self._ts()),
                )
                cur.execute(
                    "UPDATE bookings SET status='transferred',compensation=?,"
                    "version=version+1,updated_ts=? WHERE booking_id=?",
                    (amount, self._ts(), booking_id),
                )
                self._decision(
                    cur, target_session_id, "allocated", bk["seats"],
                    RULE["TRANSFER"],
                    f"补偿性转场：由 {bk['session_id']} 整体转入",
                    "confirmed", booking_id=new_id, team_id=bk["team_id"],
                )
                final = f"transfer:{new_id}+compensation:{amount}"
            self._decision(
                cur, bk["session_id"], "resolved", bk["seats"],
                RULE["COMPENSATION"],
                f"补偿决定：{arrangement}，补偿金额 {amount} 分", final,
                booking_id=booking_id, team_id=bk["team_id"],
            )
            self._timeline(
                cur, bk["session_id"], actor, "compensation_decided",
                f"预约 {booking_id} 最终安排：{arrangement}",
                {"booking_id": booking_id, "arrangement": arrangement,
                 "amount": amount, "final": final},
            )
            return {"booking_id": booking_id, "arrangement": arrangement,
                    "final": final}

    # ------------------------------------------------------------------ #
    # 现场核验（景区工作人员只接触本场次名单）
    # ------------------------------------------------------------------ #

    def roster(self, actor: Actor, session_id: str) -> dict:
        actor.require(P_ROSTER_READ)
        with self.db.read() as cur:
            session = self._row(
                cur, "SELECT * FROM sessions WHERE session_id=?", (session_id,)
            )
            actor.require_venue(session["venue_id"])
            cur.execute(
                "SELECT b.booking_id,b.team_id,b.seats,b.status,"
                "t.name AS team_name, t.agency_id, "
                "(SELECT COUNT(*) FROM admissions a WHERE a.booking_id=b.booking_id)"
                " AS admitted FROM bookings b JOIN teams t ON b.team_id=t.team_id "
                "WHERE b.session_id=? ORDER BY b.created_ts",
                (session_id,),
            )
            bookings = [dict(r) for r in cur.fetchall()]
        return {"session_id": session_id, "bookings": bookings}

    def admit(self, actor: Actor, booking_id: str, visitor_name: str,
              credential: str) -> dict:
        actor.require(P_CHECKIN)
        self._finalize_expired()
        with self.db.read() as cur:
            bk = self._row(
                cur, "SELECT * FROM bookings WHERE booking_id=?", (booking_id,)
            )
            session = self._row(
                cur, "SELECT * FROM sessions WHERE session_id=?",
                (bk["session_id"],),
            )
            actor.require_venue(session["venue_id"])
            denied = session["status"] != "scheduled"
        if denied:
            # 拒入记录在独立事务中落库，不受随后拒绝请求的影响
            with self.db.tx() as cur:
                self._timeline(
                    cur, bk["session_id"], actor, "checkin_denied",
                    f"预约 {booking_id} 入场被拒：场次 {session['status']}",
                    {"booking_id": booking_id, "credential": credential},
                )
                self._decision(
                    cur, bk["session_id"], "rejected", 1,
                    RULE["CHECKIN_DENIED"],
                    f"场次 {session['status']}，现场核验拒绝入场/补录",
                    "denied",
                    booking_id=booking_id, team_id=bk["team_id"],
                )
            raise SessionClosedError(
                f"场次已 {session['status']}，不得入场或补录"
            )
        if bk["status"] != "confirmed":
            raise ConflictError(f"预约状态 {bk['status']}，不可入场")
        with self.db.tx() as cur:
            # 写事务内复核状态，防止读与写之间被并发退款/取消
            row = self._row(
                cur, "SELECT s.status AS sstatus, b.status AS bstatus,"
                " b.seats AS seats FROM bookings b JOIN sessions s "
                "ON b.session_id=s.session_id WHERE b.booking_id=?",
                (booking_id,),
            )
            if row["sstatus"] != "scheduled":
                raise SessionClosedError("场次已关闭，不得入场")
            if row["bstatus"] != "confirmed":
                raise ConflictError(f"预约状态 {row['bstatus']}，不可入场")
            cur.execute(
                "SELECT COUNT(*) AS n FROM admissions WHERE booking_id=?",
                (booking_id,),
            )
            if cur.fetchone()["n"] >= row["seats"]:
                raise ConflictError("该预约的入场人数已达预约人数，不能再加人")
            cur.execute(
                "SELECT 1 FROM admissions WHERE booking_id=? AND credential=?",
                (booking_id, credential),
            )
            if cur.fetchone():
                return {"booking_id": booking_id, "credential": credential,
                        "status": "already_admitted"}
            admission_id = _uid("adm")
            cur.execute(
                "INSERT INTO admissions VALUES (?,?,?,?,?,?)",
                (admission_id, booking_id, visitor_name, credential,
                 self._ts(), actor.name),
            )
            return {"admission_id": admission_id, "status": "admitted"}

    # ------------------------------------------------------------------ #
    # 时间线与逐名额解释
    # ------------------------------------------------------------------ #

    def timeline(self, actor: Actor, session_id: str) -> dict:
        actor.require(P_TIMELINE_READ)
        with self.db.read() as cur:
            self._row(cur, "SELECT 1 FROM sessions WHERE session_id=?",
                      (session_id,))
            cur.execute(
                "SELECT event_id,ts,actor_role,actor_name,event_type,summary,"
                "payload FROM timeline_events WHERE session_id=? ORDER BY ts",
                (session_id,),
            )
            events = [dict(r) for r in cur.fetchall()]
        return {"session_id": session_id, "events": events}

    def explain_session(self, actor: Actor, session_id: str) -> dict:
        """运营视图：每个名额为何这样分配、哪条规则触发、最终安排。"""
        actor.require(P_TIMELINE_READ)
        with self.db.read() as cur:
            session = dict(self._row(
                cur, "SELECT * FROM sessions WHERE session_id=?", (session_id,)
            ))
            held = self._held(cur, session_id)
            cur.execute(
                "SELECT b.*, tt.name AS ticket_name, t.name AS team_name,"
                " t.agency_id FROM bookings b JOIN ticket_types tt "
                "ON b.ticket_type_id=tt.ticket_type_id JOIN teams t "
                "ON b.team_id=t.team_id WHERE b.session_id=? "
                "ORDER BY b.created_ts",
                (session_id,),
            )
            bookings = []
            rules_triggered: dict[str, int] = {}
            for r in cur.fetchall():
                b = dict(r)
                cur.execute(
                    "SELECT ts,decision,seats,rule_code,reason,"
                    "final_arrangement,payload FROM allocation_decisions "
                    "WHERE booking_id=? ORDER BY ts",
                    (b["booking_id"],),
                )
                decisions = [dict(d) for d in cur.fetchall()]
                for d in decisions:
                    rules_triggered[d["rule_code"]] = (
                        rules_triggered.get(d["rule_code"], 0) + 1
                    )
                b["decisions"] = decisions
                b["why"] = (
                    f"{decisions[-1]['reason']} → 最终安排："
                    f"{decisions[-1]['final_arrangement']}"
                    if decisions else "无决策记录"
                )
                bookings.append(b)
            cur.execute(
                "SELECT ts,actor_role,actor_name,event_type,summary,payload "
                "FROM timeline_events WHERE session_id=? ORDER BY ts",
                (session_id,),
            )
            events = [dict(e) for e in cur.fetchall()]
        return {
            "session": {
                **session,
                "held_seats": held,
                "available": session["safety_capacity"] - held,
            },
            "rules_triggered": rules_triggered,
            "bookings": bookings,
            "timeline": events,
        }

    # ------------------------------------------------------------------ #
    # 崩溃恢复
    # ------------------------------------------------------------------ #

    def recover(self) -> dict:
        """服务重启后调用：

        1. 结束时间已过的场次保持 finished（不会因迟到处理重开）；
        2. 未完成的退款重新向支付网关发起；
        3. 因中断未递补的候补按当前容量继续递补。
        """
        settled: list[str] = []
        promoted: list[dict] = []
        with self.db.tx() as cur:
            self._close_expired(cur)
            cur.execute("SELECT refund_id FROM refunds WHERE status='pending'")
            pending = [r["refund_id"] for r in cur.fetchall()]
            for refund_id in pending:
                if self._settle_refund(cur, refund_id):
                    settled.append(refund_id)
            cur.execute(
                "SELECT DISTINCT session_id FROM bookings WHERE status='waiting'"
            )
            for r in cur.fetchall():
                promoted.extend(self._promote_waitlist(cur, r["session_id"]))
        return {"refunds_settled": settled, "waitlist_promoted": promoted}
