"""容量调度服务：活动场次、场地容量、票种、团队预约、现场核验的统一入口。

设计要点
========
1. 容量安全：所有占用/释放名额的操作都在单个 ``BEGIN IMMEDIATE`` 事务里
   完成"读余量 → 判定 → 写状态"，SQLite 写锁把并发写串行化，配合
   ``safe_capacity`` 上界检查，售票/退款/候补/核验在并发下不超卖、
   退款释放的名额不会被重复卖出（名额不是计数器增减，而是以
   "confirmed 预约合计 ≤ safe_capacity"的事实重算）。

2. 整团原子：一个团队一次下单只产生一条预约，qty=团队人数；确认/候补/
   转场/退款都作用于整条预约，杜绝部分成员越过安全上限（S01）。

3. 统一时间线：安全调整、取消、转场、补偿、拒绝全部写 timeline，
   explain_session 可回放每个名额"为何分配、哪条规则限制、最终安排"。

4. 终态冻结与迟到回执：场次 finished/cancelled 后，迟到的退款/转场
   回执被拒并留痕，绝不重开（S04/S12）。

5. 崩溃继续：refunds(pending) 与 reservations(waitlisted) 持久化，
   resume_pending() 在服务重启后继续处理，逻辑幂等（S08/S09）。
"""

from __future__ import annotations

import sqlite3
import uuid
from datetime import datetime, timezone
from typing import Any

from .models import (
    Actor,
    RefundStatus,
    ReservationStatus,
    Role,
    SessionState,
    TicketKind,
    TimelineKind,
)
from .rules import (
    AUTH_DISPATCHER_CAPACITY,
    AUTH_SITE_STAFF_SESSION_SCOPED,
    AUTH_TRAVEL_AGENCY_OWN_TEAMS,
    CAPACITY_ATOMIC_TEAM,
    CAPACITY_FREE_QUOTA,
    CAPACITY_SAFE_ADJUST,
    CAPACITY_SAFE_ADJUST_FROZEN,
    CHECKIN_NOT_CANCELLED,
    CHECKIN_ONCE,
    CHECKIN_SCOPED,
    COMPENSATION_DECISION,
    DomainError,
    PolicyError,
    REFUND_CANCELLED,
    REFUND_IDEMPOTENT,
    REFUND_LATE_ACK_REJECTED,
    REFUND_SAFE_ADJUST,
    REROLL_TO_ALT_SESSION,
    RULES,
    SAFETY_CAP_HARD_LIMIT,
    TICKET_SOLD_OUT,
    WAITLIST_FIFO,
    WAITLIST_RESUME,
)
from .store import Store


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _new_id(prefix: str) -> str:
    return f"{prefix}_{uuid.uuid4().hex[:12]}"


class CapacityService:
    def __init__(self, store: Store):
        self.store = store

    # ===================================================================
    # 基础资料：场地 / 场次 / 票种 / 团队
    # ===================================================================
    def create_venue(self, venue_id: str, name: str, region: str) -> None:
        with self.store.write() as c:
            c.execute(
                "INSERT INTO venues(venue_id, name, region) VALUES(?,?,?)",
                (venue_id, name, region),
            )

    def create_session(
        self,
        session_id: str,
        venue_id: str,
        title: str,
        activity_type: str,
        start_ts: str,
        end_ts: str,
        safe_capacity: int,
        actor: Actor | None = None,
    ) -> None:
        """登记活动场次。建场次属调度员排期权限。"""
        if actor is not None and actor.role is not Role.DISPATCHER:
            raise PolicyError(AUTH_DISPATCHER_CAPACITY, "只有调度员可以排期建场次")
        if safe_capacity <= 0:
            raise DomainError("安全容量必须为正数")
        with self.store.write() as c:
            c.execute(
                """INSERT INTO sessions(session_id, venue_id, title, activity_type,
                       start_ts, end_ts, state, safe_capacity, original_capacity, created_at)
                   VALUES(?,?,?,?,?,?, 'scheduled', ?, ?, ?)""",
                (session_id, venue_id, title, activity_type, start_ts, end_ts,
                 safe_capacity, safe_capacity, _now()),
            )

    def add_ticket_type(
        self,
        type_id: str,
        session_id: str,
        name: str,
        kind: TicketKind,
        total_qty: int,
        price: int = 0,
    ) -> None:
        if total_qty < 0 or price < 0:
            raise DomainError("票量与价格不能为负")
        with self.store.write() as c:
            if not c.execute("SELECT 1 FROM sessions WHERE session_id=?", (session_id,)).fetchone():
                raise DomainError(f"场次不存在：{session_id}")
            c.execute(
                """INSERT INTO ticket_types(type_id, session_id, name, kind, price, total_qty)
                   VALUES(?,?,?,?,?,?)""",
                (type_id, session_id, name, kind.value, price, total_qty),
            )

    def register_team(
        self, team_id: str, agency_code: str, leader_name: str, size: int, contact: str
    ) -> None:
        if size <= 0:
            raise DomainError("团队人数必须为正")
        with self.store.write() as c:
            c.execute(
                """INSERT INTO teams(team_id, agency_code, leader_name, size, contact)
                   VALUES(?,?,?,?,?)""",
                (team_id, agency_code, leader_name, size, contact),
            )

    # ===================================================================
    # 授权：免费名额 / 跨区域调配
    # ===================================================================
    def grant_authorization(
        self,
        actor: Actor,
        grantee_id: str,
        permission: str,
        scope_session_id: str | None,
        payload: dict | None = None,
    ) -> str:
        """只有调度员可以授予 free_quota / cross_region_reroll 权限（S17）。"""
        self._require_dispatcher(actor, "授权")
        auth_id = _new_id("auth")
        with self.store.write() as c:
            c.execute(
                """INSERT INTO authorizations(auth_id, actor_id, permission, scope_session_id,
                       payload, granted_by, granted_at)
                   VALUES(?,?,?,?,?,?,?)""",
                (auth_id, grantee_id, permission, scope_session_id,
                 Store.dumps(payload or {}), actor.actor_id, _now()),
            )
            self._add_timeline(
                c, scope_session_id or "*", None, actor.actor_id,
                TimelineKind.AUTH_GRANT, AUTH_DISPATCHER_CAPACITY,
                f"授予 {grantee_id} 权限 {permission}"
                + (f"（场次 {scope_session_id}）" if scope_session_id else "（全局）"),
                {"permission": permission, "grantee": grantee_id, "payload": payload or {}},
            )
        return auth_id

    def _has_auth(self, c: sqlite3.Connection, actor: Actor, permission: str, session_id: str) -> bool:
        # 调度员持有全部运营授权，无需给自己开授权单
        if actor.role is Role.DISPATCHER:
            return True
        row = c.execute(
            """SELECT 1 FROM authorizations
               WHERE actor_id=? AND permission=? AND revoked=0
                 AND (scope_session_id IS NULL OR scope_session_id=?)
               LIMIT 1""",
            (actor.actor_id, permission, session_id),
        ).fetchone()
        return row is not None

    # ===================================================================
    # 售票（散客与整团）
    # ===================================================================
    def book(
        self,
        actor: Actor,
        session_id: str,
        type_id: str,
        qty: int,
        *,
        visitor_name: str,
        visitor_phone: str = "",
        team_id: str | None = None,
        idem_key: str | None = None,
        preference: dict | None = None,
    ) -> dict:
        """下单。团队（team_id 非空）qty 必须等于团队人数，且整团同一条预约。

        余量充足 → confirmed；否则整单进入 waitlisted，绝不部分确认。
        idem_key 用于客户端重试/崩溃后重投，命中则返回既有结果不重复下单。
        """
        if qty <= 0:
            raise DomainError("预约人数必须为正")

        # 旅行社只能给自己的团队下单；景区岗不能售票
        if actor.role is Role.TRAVEL_AGENCY:
            if team_id is None:
                raise PolicyError(AUTH_TRAVEL_AGENCY_OWN_TEAMS, "旅行社只能预约本社团队，不能代散客下单")
        elif actor.role is Role.SITE_STAFF:
            raise PolicyError(AUTH_SITE_STAFF_SESSION_SCOPED, "景区岗位不负责售票")

        with self.store.write() as c:
            # 幂等：重投直接返回原预约
            if idem_key:
                old = c.execute(
                    "SELECT * FROM reservations WHERE idem_key=?", (idem_key,)
                ).fetchone()
                if old:
                    return dict(old)

            sess = self._get_session(c, session_id)
            tt = self._get_ticket_type(c, type_id, session_id)
            if team_id is not None:
                team = c.execute("SELECT * FROM teams WHERE team_id=?", (team_id,)).fetchone()
                if not team:
                    raise DomainError(f"团队不存在：{team_id}")
                if actor.role is Role.TRAVEL_AGENCY and team["agency_code"] != actor.travel_agency_code:
                    raise PolicyError(
                        AUTH_TRAVEL_AGENCY_OWN_TEAMS,
                        f"团队 {team_id} 不属于旅行社 {actor.travel_agency_code}",
                    )
                if qty != team["size"]:
                    raise PolicyError(
                        CAPACITY_ATOMIC_TEAM,
                        f"团队 {team_id} 共 {team['size']} 人，下单 {qty} 人；整团必须同进同退",
                    )
                visitor_name = team["leader_name"]
                visitor_phone = team["contact"]

            if tt["kind"] == TicketKind.FREE.value:
                if not self._has_auth(c, actor, "free_quota", session_id):
                    raise PolicyError(CAPACITY_FREE_QUOTA, f"免费票种 {type_id} 需授权签发")

            if sess["state"] != SessionState.SCHEDULED.value:
                raise PolicyError(
                    CAPACITY_SAFE_ADJUST_FROZEN,
                    f"场次 {session_id} 已{sess['state']}，不再接受新预约",
                )

            confirmed_qty = self._confirmed_qty(c, session_id)
            sold_qty = self._sold_qty(c, type_id)
            cap_ok = confirmed_qty + qty <= sess["safe_capacity"]
            type_ok = sold_qty + qty <= tt["total_qty"]

            rid = _new_id("res")
            if cap_ok and type_ok:
                status = ReservationStatus.CONFIRMED.value
                rule = ""
                summary = f"售票确认：{visitor_name} {qty} 人（{tt['name']}）"
                kind = TimelineKind.SALE
            else:
                status = ReservationStatus.WAITLISTED.value
                rule = TICKET_SOLD_OUT if not type_ok else SAFETY_CAP_HARD_LIMIT
                which = "票种池已满" if not type_ok else f"安全容量剩余 {sess['safe_capacity'] - confirmed_qty}"
                summary = f"进入候补：{visitor_name} {qty} 人（{tt['name']}），{which}；整团继续等待"
                kind = TimelineKind.WAITLIST

            c.execute(
                """INSERT INTO reservations(reservation_id, session_id, type_id, team_id,
                       visitor_name, visitor_phone, qty, status, unit_price, booked_by,
                       idem_key, preference, created_at, confirmed_at)
                   VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
                (rid, session_id, type_id, team_id, visitor_name, visitor_phone, qty,
                 status, tt["price"], actor.actor_id, idem_key,
                 Store.dumps(preference or {}), _now(),
                 _now() if status == ReservationStatus.CONFIRMED.value else None),
            )
            self._add_timeline(c, session_id, rid, actor.actor_id, kind, rule, summary,
                               {"qty": qty, "type_id": type_id, "team_id": team_id})
            return dict(c.execute("SELECT * FROM reservations WHERE reservation_id=?", (rid,)).fetchone())

    # ===================================================================
    # 容量守卫（每次写入后可复用的不变量）
    # ===================================================================
    @staticmethod
    def _confirmed_qty(c: sqlite3.Connection, session_id: str) -> int:
        row = c.execute(
            "SELECT COALESCE(SUM(qty),0) AS n FROM reservations WHERE session_id=? AND status='confirmed'",
            (session_id,),
        ).fetchone()
        return row["n"]

    @staticmethod
    def _sold_qty(c: sqlite3.Connection, type_id: str) -> int:
        row = c.execute(
            "SELECT COALESCE(SUM(qty),0) AS n FROM reservations WHERE type_id=? AND status='confirmed'",
            (type_id,),
        ).fetchone()
        return row["n"]

    def _assert_capacity_invariant(self, c: sqlite3.Connection, session_id: str) -> None:
        sess = self._get_session(c, session_id)
        used = self._confirmed_qty(c, session_id)
        if used > sess["safe_capacity"]:
            raise PolicyError(
                SAFETY_CAP_HARD_LIMIT,
                f"容量不变量被破坏：已确认 {used} > 安全容量 {sess['safe_capacity']}",
            )

    # ===================================================================
    # 安全容量调整（限流下调）
    # ===================================================================
    def adjust_safe_capacity(
        self, actor: Actor, session_id: str, new_capacity: int, reason: str
    ) -> dict:
        """调度员因安全原因下调容量；超出部分按"最晚确认先挤出"处理。

        被挤出的预约进入待退款（pending），随后在同事务内尝试按偏好转场；
        转场成功的标记 rerolled，其余由 resume/refund 流程全额退款（S03/S10/S13）。
        """
        self._require_dispatcher(actor, "安全容量调整")
        if new_capacity <= 0:
            raise DomainError("安全容量必须为正数")

        with self.store.write() as c:
            sess = self._get_session(c, session_id)
            if sess["state"] != SessionState.SCHEDULED.value:
                raise PolicyError(
                    CAPACITY_SAFE_ADJUST_FROZEN,
                    f"场次已{sess['state']}，安全调整被拒绝",
                )
            old = sess["safe_capacity"]
            if new_capacity > sess["original_capacity"]:
                raise PolicyError(
                    CAPACITY_SAFE_ADJUST,
                    f"安全容量恢复不得超过原始容量 {sess['original_capacity']}",
                )
            if new_capacity == old:
                return {"changed": False}

            c.execute(
                "UPDATE sessions SET safe_capacity=? WHERE session_id=?",
                (new_capacity, session_id),
            )
            direction = "限流下调" if new_capacity < old else "限流解除、容量恢复"
            self._add_timeline(
                c, session_id, None, actor.actor_id, TimelineKind.CAPACITY_ADJUST,
                CAPACITY_SAFE_ADJUST,
                f"安全容量 {old} → {new_capacity}（{direction}），原因：{reason}",
                {"old": old, "new": new_capacity, "reason": reason},
            )

            result = {"changed": True, "old": old, "new": new_capacity,
                      "displaced": [], "rerolled": [], "refunds_queued": []}

            if new_capacity < old:
                overflow = self._confirmed_qty(c, session_id) - new_capacity
                displaced: list[sqlite3.Row] = []
                if overflow > 0:
                    # 最晚确认的先挤出，直到不超限
                    rows = c.execute(
                        """SELECT * FROM reservations
                           WHERE session_id=? AND status='confirmed'
                           ORDER BY confirmed_at DESC, rowid DESC""",
                        (session_id,),
                    ).fetchall()
                    for r in rows:
                        if overflow <= 0:
                            break
                        displaced.append(r)
                        overflow -= r["qty"]

                for r in displaced:
                    self._displace_one(c, actor, r, reason, result,
                                       cancel_session=False)

            # 容量恢复或挤出退款后都可能腾出空间，唤醒本场次候补
            promoted = self._promote_waitlist(c, actor, session_id)
            result["waitlist_promoted"] = promoted
            self._assert_capacity_invariant(c, session_id)
            return result

    def _displace_one(
        self,
        c: sqlite3.Connection,
        actor: Actor,
        res: sqlite3.Row,
        reason: str,
        result: dict | None,
        *,
        cancel_session: bool,
    ) -> str:
        """挤出/取消单条已确认预约：先尝试转场，否则入退款队列。返回处置结果。"""
        rid = res["reservation_id"]
        session_id = res["session_id"]
        pref = Store.loads(res["preference"])

        target = self._find_reroll_target(c, res, pref, actor) if pref.get("accept_reroll") else None
        if target is not None:
            self._do_reroll(c, actor, res, target, reason,
                            REFUND_CANCELLED if cancel_session else REFUND_SAFE_ADJUST)
            if result is not None:
                result["rerolled"].append({"reservation_id": rid, "target": target})
                result["displaced"].append(rid)
            return "rerolled"

        # 转场不可行 → 挂起确认名额，进入退款 outbox（pending）
        c.execute(
            "UPDATE reservations SET status=? WHERE reservation_id=?",
            (ReservationStatus.CANCELLED_BY_OPS.value, rid),
        )
        refund_id = self._queue_refund(
            c, rid,
            reason=("cancel:" + reason) if cancel_session else "capacity:" + reason,
            amount=res["unit_price"] * res["qty"],
            rule=REFUND_CANCELLED if cancel_session else REFUND_SAFE_ADJUST,
        )
        self._add_timeline(
            c, session_id, rid, actor.actor_id, TimelineKind.REFUND,
            REFUND_CANCELLED if cancel_session else REFUND_SAFE_ADJUST,
            f"名额收回并排队全额退款 {res['unit_price'] * res['qty']/100:.2f} 元"
            f"（{res['visitor_name']} {res['qty']} 人，{reason}）",
            {"refund_id": refund_id, "amount": res["unit_price"] * res["qty"]},
        )
        if result is not None:
            result["refunds_queued"].append(refund_id)
            result["displaced"].append(rid)
        return "refund_queued"

    # ===================================================================
    # 取消场次
    # ===================================================================
    def cancel_session(self, actor: Actor, session_id: str, reason: str) -> dict:
        """调度员取消场次：终态化、全部有效预约退款或按偏好转场（S11/S13）。"""
        self._require_dispatcher(actor, "取消场次")
        with self.store.write() as c:
            sess = self._get_session(c, session_id)
            if sess["state"] == SessionState.CANCELLED.value:
                return {"changed": False, "reason": "already_cancelled"}
            if sess["state"] == SessionState.FINISHED.value:
                raise PolicyError(CAPACITY_SAFE_ADJUST_FROZEN, "已结束场次不能取消")

            result = {"changed": True, "displaced": [], "rerolled": [],
                      "refunds_queued": [], "waitlist_rejected": []}
            confirmed = c.execute(
                "SELECT * FROM reservations WHERE session_id=? AND status='confirmed'",
                (session_id,),
            ).fetchall()
            for r in confirmed:
                self._displace_one(c, actor, r, reason, result, cancel_session=True)

            # 候补单不再有兑现可能：作废并留痕（不退款，未收款）
            waiting = c.execute(
                "SELECT * FROM reservations WHERE session_id=? AND status='waitlisted'",
                (session_id,),
            ).fetchall()
            for r in waiting:
                c.execute(
                    "UPDATE reservations SET status=? WHERE reservation_id=?",
                    (ReservationStatus.REJECTED.value, r["reservation_id"]),
                )
                self._add_timeline(
                    c, session_id, r["reservation_id"], actor.actor_id,
                    TimelineKind.CANCEL, REFUND_CANCELLED,
                    f"候补作废：{r['visitor_name']} {r['qty']} 人（场次取消，未收款）",
                    {"reason": reason},
                )
                result["waitlist_rejected"].append(r["reservation_id"])

            c.execute(
                "UPDATE sessions SET state='cancelled' WHERE session_id=?",
                (session_id,),
            )
            self._add_timeline(
                c, session_id, None, actor.actor_id, TimelineKind.CANCEL,
                REFUND_CANCELLED, f"场次取消：{reason}",
                {"reason": reason, "affected_confirmed": len(confirmed),
                 "waitlist": len(waiting)},
            )
            return result

    # ===================================================================
    # 结束场次（前一天的场次到期）
    # ===================================================================
    def finish_session(self, actor: Actor, session_id: str) -> None:
        self._require_dispatcher(actor, "结束场次")
        with self.store.write() as c:
            sess = self._get_session(c, session_id)
            if sess["state"] != SessionState.SCHEDULED.value:
                raise PolicyError(CAPACITY_SAFE_ADJUST_FROZEN, f"场次当前状态 {sess['state']}")
            # 在场候补随结束作废（未占名额、未收款）
            waiting = c.execute(
                "SELECT * FROM reservations WHERE session_id=? AND status='waitlisted'",
                (session_id,),
            ).fetchall()
            for r in waiting:
                c.execute(
                    "UPDATE reservations SET status='rejected' WHERE reservation_id=?",
                    (r["reservation_id"],),
                )
            c.execute("UPDATE sessions SET state='finished' WHERE session_id=?", (session_id,))
            self._add_timeline(c, session_id, None, actor.actor_id,
                               TimelineKind.FINISH, CAPACITY_SAFE_ADJUST_FROZEN,
                               f"场次正常结束并冻结，{len(waiting)} 条候补随终态作废",
                               {"waitlist_dropped": len(waiting)})

    # ===================================================================
    # 转场
    # ===================================================================
    def _find_reroll_target(
        self,
        c: sqlite3.Connection,
        res: sqlite3.Row,
        pref: dict,
        actor: Actor,
    ) -> str | None:
        """为整团找一个有余量的同类场次；跨区域需授权（S13/S17）。"""
        sess = self._get_session(c, res["session_id"])
        src_venue = c.execute(
            "SELECT region FROM venues WHERE venue_id=?", (sess["venue_id"],)
        ).fetchone()["region"]
        rows = c.execute(
            """SELECT s.*, v.region FROM sessions s JOIN venues v ON s.venue_id=v.venue_id
               WHERE s.state='scheduled' AND s.session_id != ?
               ORDER BY s.start_ts""",
            (res["session_id"],),
        ).fetchall()
        same_type = pref.get("same_activity_type", True)
        want_region = pref.get("region")  # 指定偏好区域
        for t in rows:
            if same_type and t["activity_type"] != sess["activity_type"]:
                continue
            cross_region = t["region"] != src_venue
            if cross_region and not self._has_auth(c, actor, "cross_region_reroll", t["session_id"]):
                continue  # 无跨区授权，跳过
            if want_region and t["region"] != want_region:
                continue
            # 容量与票种池都要容得下整团；票种按同类型匹配（免费票只转免费场，
            # 不产生新费用；付费票选同类型中最便宜的）
            if self._confirmed_qty(c, t["session_id"]) + res["qty"] > t["safe_capacity"]:
                continue
            src_tt = c.execute("SELECT kind FROM ticket_types WHERE type_id=?",
                               (res["type_id"],)).fetchone()
            tt = c.execute(
                """SELECT * FROM ticket_types WHERE session_id=? AND kind=?
                   ORDER BY price LIMIT 1""",
                (t["session_id"], src_tt["kind"]),
            ).fetchone()
            if tt is None or self._sold_qty(c, tt["type_id"]) + res["qty"] > tt["total_qty"]:
                continue
            return t["session_id"]
        return None

    def _do_reroll(
        self,
        c: sqlite3.Connection,
        actor: Actor,
        res: sqlite3.Row,
        target_session_id: str,
        reason: str,
        rule: str,
    ) -> str:
        tt = c.execute(
            "SELECT * FROM ticket_types WHERE session_id=? ORDER BY price LIMIT 1",
            (target_session_id,),
        ).fetchone()
        new_rid = _new_id("res")
        c.execute(
            """INSERT INTO reservations(reservation_id, session_id, type_id, team_id,
                   visitor_name, visitor_phone, qty, status, unit_price, booked_by,
                   idem_key, reroll_from, preference, created_at, confirmed_at)
               VALUES(?,?,?,?,?,?,?, 'confirmed', ?, ?, NULL, ?, ?, ?, ?)""",
            (new_rid, target_session_id, tt["type_id"], res["team_id"],
             res["visitor_name"], res["visitor_phone"], res["qty"], tt["price"],
             actor.actor_id, res["reservation_id"], res["preference"], _now(), _now()),
        )
        c.execute(
            "UPDATE reservations SET status=? WHERE reservation_id=?",
            (ReservationStatus.REROLLED.value, res["reservation_id"]),
        )
        self._add_timeline(
            c, res["session_id"], res["reservation_id"], actor.actor_id,
            TimelineKind.REROLL, REROLL_TO_ALT_SESSION,
            f"整团转场：{res['visitor_name']} {res['qty']} 人 → 场次 {target_session_id}（{reason}）",
            {"from": res["session_id"], "to": target_session_id,
             "new_reservation_id": new_rid, "rule_context": rule},
        )
        self._add_timeline(
            c, target_session_id, new_rid, actor.actor_id,
            TimelineKind.REROLL, REROLL_TO_ALT_SESSION,
            f"接收入转：{res['visitor_name']} {res['qty']} 人（自 {res['session_id']}）",
            {"from": res["session_id"]},
        )
        return new_rid

    def reroll_reservation(
        self, actor: Actor, reservation_id: str, target_session_id: str
    ) -> str:
        """旅行社可为自己的团队申请转场；跨区域目标需调度员授权。"""
        with self.store.write() as c:
            res = self._get_reservation(c, reservation_id)
            self._authorize_reservation_action(actor, res, c, "转场")
            tgt = self._get_session(c, target_session_id)
            src = self._get_session(c, res["session_id"])
            if tgt["state"] != SessionState.SCHEDULED.value:
                raise PolicyError(CAPACITY_SAFE_ADJUST_FROZEN, "目标场次不在可预约状态")
            src_region = c.execute(
                "SELECT region FROM venues WHERE venue_id=?", (src["venue_id"],)
            ).fetchone()["region"]
            tgt_region = c.execute(
                "SELECT region FROM venues WHERE venue_id=?", (tgt["venue_id"],)
            ).fetchone()["region"]
            if src_region != tgt_region and not self._has_auth(
                c, actor, "cross_region_reroll", target_session_id
            ):
                raise PolicyError(AUTH_DISPATCHER_CAPACITY,
                                  f"{src_region} → {tgt_region} 的跨区域调配需调度员授权")
            if self._confirmed_qty(c, target_session_id) + res["qty"] > tgt["safe_capacity"]:
                raise PolicyError(SAFETY_CAP_HARD_LIMIT, "目标场次容量不足以容纳整团")
            # 只有已确认预约需要占/释放；候补单转场直接搬单
            new_rid = self._do_reroll(c, actor, res, target_session_id, "团队申请", REROLL_TO_ALT_SESSION)
            self._promote_waitlist(c, actor, res["session_id"])
            return new_rid

    # ===================================================================
    # 候补：FIFO + 整团全有 + 唤醒
    # ===================================================================
    def _promote_waitlist(
        self, c: sqlite3.Connection, actor: Actor, session_id: str
    ) -> list[str]:
        """释放名额后按 created_at 先进先出尝试；整团放不下就跳过，继续后面的。"""
        promoted: list[str] = []
        while True:
            sess = self._get_session(c, session_id)
            if sess["state"] != SessionState.SCHEDULED.value:
                break
            nxt = c.execute(
                """SELECT * FROM reservations
                   WHERE session_id=? AND status='waitlisted'
                   ORDER BY created_at ASC, rowid ASC LIMIT 1""",
                (session_id,),
            ).fetchone()
            if nxt is None:
                break
            tt = c.execute("SELECT * FROM ticket_types WHERE type_id=?", (nxt["type_id"],)).fetchone()
            cap_room = sess["safe_capacity"] - self._confirmed_qty(c, session_id)
            type_room = tt["total_qty"] - self._sold_qty(c, tt["type_id"])
            if nxt["qty"] <= cap_room and nxt["qty"] <= type_room:
                c.execute(
                    "UPDATE reservations SET status='confirmed', confirmed_at=? WHERE reservation_id=?",
                    (_now(), nxt["reservation_id"]),
                )
                self._add_timeline(
                    c, session_id, nxt["reservation_id"], "system",
                    TimelineKind.SALE, WAITLIST_FIFO,
                    f"候补自动确认：{nxt['visitor_name']} {nxt['qty']} 人（FIFO，整团全有）",
                    {"from_waitlist": True},
                )
                promoted.append(nxt["reservation_id"])
                continue
            # 队首整团放不下：按 FIFO 不跳过（排在前面的人继续等，避免饥饿），
            # 但若后续更小的散客单能放下也不确认——严格 FIFO 语义。
            break
        return promoted

    # ===================================================================
    # 退款（自愿退 / outbox / 幂等 / 迟到回执）
    # ===================================================================
    def request_refund(
        self, actor: Actor, reservation_id: str, reason: str, idem_key: str
    ) -> dict:
        """游客/旅行社发起退款。终态场次后的迟到回执被拒并留痕（S12）。"""
        with self.store.write() as c:
            res = self._get_reservation(c, reservation_id)
            self._authorize_reservation_action(actor, res, c, "退款")
            sess = self._get_session(c, res["session_id"])

            existing = c.execute(
                "SELECT * FROM refunds WHERE idem_key=?", (idem_key,)
            ).fetchone()
            if existing:
                return dict(existing)
            dup = c.execute(
                "SELECT * FROM refunds WHERE reservation_id=? AND status IN ('done','pending')",
                (reservation_id,),
            ).fetchone()
            if dup:
                raise PolicyError(REFUND_IDEMPOTENT,
                                  f"预约 {reservation_id} 已存在退款单 {dup['refund_id']}，不重复退款")

            # 典型场景：场次前一天已结束，渠道退款回执今天才到 —— 先拦终态，不重开
            if sess["state"] in (SessionState.FINISHED.value, SessionState.CANCELLED.value) \
                    and res["status"] not in (
                        ReservationStatus.CANCELLED_BY_OPS.value,
                        ReservationStatus.REFUNDED.value,
                        ReservationStatus.REROLLED.value,
                        ReservationStatus.REJECTED.value,
                    ):
                return self._reject_late_ack(c, actor, res, sess, idem_key, reason)

            if res["status"] not in (
                ReservationStatus.CONFIRMED.value,
                ReservationStatus.WAITLISTED.value,
            ):
                raise PolicyError(REFUND_IDEMPOTENT,
                                  f"预约状态 {res['status']} 不可退款")

            if res["status"] == ReservationStatus.WAITLISTED.value:
                # 候补未占名额、未收款：直接关闭，不产生资金流
                c.execute("UPDATE reservations SET status='rejected' WHERE reservation_id=?", (reservation_id,))
                rf = {
                    "refund_id": _new_id("rf"), "reservation_id": reservation_id,
                    "reason": reason, "amount": 0, "status": RefundStatus.DONE.value,
                }
                c.execute(
                    """INSERT INTO refunds(refund_id, reservation_id, reason, amount,
                           status, rule, idem_key, requested_at, processed_at)
                       VALUES(?,?,?,?,'done',?,?,?,?)""",
                    (rf["refund_id"], reservation_id, reason, 0, REFUND_IDEMPOTENT,
                     idem_key, _now(), _now()),
                )
                self._add_timeline(c, res["session_id"], reservation_id, actor.actor_id,
                                   TimelineKind.REFUND, REFUND_IDEMPOTENT,
                                   f"候补退出：{res['visitor_name']}（未收款）", {})
                # 候补退出后队首变化，唤醒后续候补
                self._promote_waitlist(c, Actor.dispatcher("system"), res["session_id"])
                return rf

            refund_id = self._queue_refund(
                c, reservation_id, reason="voluntary:" + reason,
                amount=res["unit_price"] * res["qty"], rule=REFUND_IDEMPOTENT,
                idem_key=idem_key, actor_id=actor.actor_id,
            )
            self._process_refund(c, c.execute(
                "SELECT * FROM refunds WHERE refund_id=?", (refund_id,)).fetchone())
            return dict(c.execute("SELECT * FROM refunds WHERE refund_id=?", (refund_id,)).fetchone())

    def _queue_refund(
        self,
        c: sqlite3.Connection,
        reservation_id: str,
        *,
        reason: str,
        amount: int,
        rule: str,
        idem_key: str | None = None,
        actor_id: str = "system",
    ) -> str:
        """入退款 outbox。UNIQUE(reservation_id, reason) 保证同因不重。"""
        refund_id = _new_id("rf")
        try:
            c.execute(
                """INSERT INTO refunds(refund_id, reservation_id, reason, amount,
                       status, rule, idem_key, requested_at)
                   VALUES(?,?,?,?,'pending',?,?,?)""",
                (refund_id, reservation_id, reason, amount, rule,
                 idem_key or _new_id("idem"), _now()),
            )
        except sqlite3.IntegrityError as e:
            raise PolicyError(REFUND_IDEMPOTENT, f"同一预约同一原因已有退款单：{reason}") from e
        return refund_id

    def _process_refund(self, c: sqlite3.Connection, rf: sqlite3.Row) -> list[str]:
        """处理一笔 pending 退款：幂等。成功则释放名额并唤醒候补。

        返回本次被 FIFO 提升的候补预约 id 列表（无提升为空列表）。
        """
        if rf["status"] != RefundStatus.PENDING.value:
            return []
        res = self._get_reservation(c, rf["reservation_id"])
        # 已确认 → 释放名额（以状态变化体现，而非计数），候补随后顶上
        if res["status"] == ReservationStatus.CONFIRMED.value:
            c.execute(
                "UPDATE reservations SET status='refunded' WHERE reservation_id=?",
                (res["reservation_id"],),
            )
        elif res["status"] in (
            ReservationStatus.CANCELLED_BY_OPS.value,
            ReservationStatus.WAITLISTED.value,
        ):
            c.execute(
                "UPDATE reservations SET status='refunded' WHERE reservation_id=?",
                (res["reservation_id"],),
            )
        else:  # rerolled / refunded / rejected：不应再退
            c.execute(
                "UPDATE refunds SET status='rejected', processed_at=?, rule=? WHERE refund_id=?",
                (_now(), REFUND_LATE_ACK_REJECTED, rf["refund_id"]),
            )
            return []

        c.execute(
            "UPDATE refunds SET status='done', processed_at=? WHERE refund_id=?",
            (_now(), rf["refund_id"]),
        )
        self._add_timeline(
            c, res["session_id"], res["reservation_id"], "system",
            TimelineKind.REFUND, rf["rule"] or REFUND_IDEMPOTENT,
            f"退款完成 {rf['amount']/100:.2f} 元，名额释放（{rf['reason']}）",
            {"refund_id": rf["refund_id"], "amount": rf["amount"]},
        )
        return self._promote_waitlist(c, Actor.dispatcher("system"), res["session_id"])

    def _reject_late_ack(
        self, c: sqlite3.Connection, actor: Actor, res: sqlite3.Row,
        sess: sqlite3.Row, idem_key: str, reason: str,
    ) -> dict:
        rf = {
            "refund_id": _new_id("rf"), "reservation_id": res["reservation_id"],
            "reason": "late_ack:" + reason, "amount": 0,
            "status": RefundStatus.REJECTED.value,
        }
        c.execute(
            """INSERT INTO refunds(refund_id, reservation_id, reason, amount,
                   status, rule, idem_key, requested_at, processed_at)
               VALUES(?,?,?,?,'rejected',?,?,?,?)""",
            (rf["refund_id"], res["reservation_id"], "late_ack:" + reason, 0,
             REFUND_LATE_ACK_REJECTED, idem_key, _now(), _now()),
        )
        self._add_timeline(
            c, sess["session_id"], res["reservation_id"], actor.actor_id,
            TimelineKind.REJECTED_ACK, REFUND_LATE_ACK_REJECTED,
            f"拒绝迟到回执：场次已{sess['state']}，预约 {res['reservation_id']} 状态 {res['status']}，不重开场次",
            {"incoming_reason": reason},
        )
        return rf

    def resume_pending(self) -> dict:
        """服务（重）启动后继续处理：先补发退款，再重放候补（S08）。

        - 退款 outbox 中所有 pending 逐笔幂等处理；
        - 各 scheduled 场次的候补队列重新尝试 FIFO 确认；
        - finished/cancelled 场次一律不动（终态冻结）。
        """
        out = {"refunds_processed": [], "refunds_rejected": [], "waitlist_promoted": {}}
        with self.store.write() as c:
            pending = c.execute(
                "SELECT * FROM refunds WHERE status='pending' ORDER BY requested_at"
            ).fetchall()
            promoted_map: dict[str, list[str]] = {}
            for rf in pending:
                # 终态前已合法挂起的退款继续执行：退款不改场次状态，且
                # _promote_waitlist 对非 scheduled 场次立即跳过，不会重开。
                promoted = self._process_refund(c, rf)
                final = c.execute(
                    "SELECT status FROM refunds WHERE refund_id=?", (rf["refund_id"],)
                ).fetchone()["status"]
                if final == RefundStatus.DONE.value:
                    out["refunds_processed"].append(rf["refund_id"])
                elif final == RefundStatus.REJECTED.value:
                    out["refunds_rejected"].append(rf["refund_id"])
                for pid in promoted:
                    sid = self._get_reservation(c, pid)["session_id"]
                    promoted_map.setdefault(sid, [])
                    if pid not in promoted_map[sid]:
                        promoted_map[sid].append(pid)

            # 再扫描仍有候补的场次（含没有挂起退款、但名额此前已释放的情况）
            for sid_row in c.execute(
                "SELECT DISTINCT session_id FROM reservations WHERE status='waitlisted'"
            ).fetchall():
                sid = sid_row["session_id"]
                for pid in self._promote_waitlist(c, Actor.dispatcher("system"), sid):
                    promoted_map.setdefault(sid, [])
                    if pid not in promoted_map[sid]:
                        promoted_map[sid].append(pid)
            out["waitlist_promoted"] = promoted_map
            self._add_timeline(
                c, "*", None, "system", TimelineKind.REFUND, WAITLIST_RESUME,
                "服务恢复：挂起退款与候补队列已继续处理",
                {k: v for k, v in out.items() if v},
            )
        return out

    # ===================================================================
    # 现场核验
    # ===================================================================
    def check_in(self, actor: Actor, reservation_id: str) -> dict:
        """景区工作人员核验入场；只能操作自己岗的场次，每人一次（S18/S19/S20）。"""
        if actor.role not in (Role.SITE_STAFF, Role.DISPATCHER):
            raise PolicyError(CHECKIN_SCOPED, "只有本场次景区工作人员可以核验")
        with self.store.write() as c:
            res = self._get_reservation(c, reservation_id)
            sess = self._get_session(c, res["session_id"])
            if actor.role is Role.SITE_STAFF and actor.staff_session_id != res["session_id"]:
                raise PolicyError(
                    CHECKIN_SCOPED,
                    f"该工作人员岗位在场次 {actor.staff_session_id}，不能核验场次 {res['session_id']}",
                )
            if sess["state"] == SessionState.CANCELLED.value:
                raise PolicyError(CHECKIN_NOT_CANCELLED, "场次已取消，不得核验")
            if sess["state"] == SessionState.FINISHED.value:
                raise PolicyError(CHECKIN_NOT_CANCELLED, "场次已结束，不得核验")
            if res["status"] != ReservationStatus.CONFIRMED.value:
                raise PolicyError(CHECKIN_ONCE, f"预约状态 {res['status']}，不可入场")
            exists = c.execute(
                "SELECT 1 FROM checkins WHERE reservation_id=?", (reservation_id,)
            ).fetchone()
            if exists:
                raise PolicyError(CHECKIN_ONCE, "该预约已核验，请勿重复扫码")
            c.execute(
                "INSERT INTO checkins(reservation_id, session_id, checked_by, ts) VALUES(?,?,?,?)",
                (reservation_id, res["session_id"], actor.actor_id, _now()),
            )
            self._add_timeline(
                c, res["session_id"], reservation_id, actor.actor_id,
                TimelineKind.CHECKIN, CHECKIN_ONCE,
                f"核验入场：{res['visitor_name']} {res['qty']} 人", {})
            return {"reservation_id": reservation_id, "checked_in": True, "qty": res["qty"]}

    def session_roster(self, actor: Actor, session_id: str) -> list[dict]:
        """景区工作人员只接触本场次名单（S15）；调度员可看任意场次。"""
        if actor.role is Role.SITE_STAFF and actor.staff_session_id != session_id:
            raise PolicyError(AUTH_SITE_STAFF_SESSION_SCOPED, "无权查看其他场次名单")
        if actor.role is Role.TRAVEL_AGENCY:
            raise PolicyError(AUTH_SITE_STAFF_SESSION_SCOPED, "旅行社不能拉取含散客信息的全场名单")
        with self.store.read() as c:
            rows = c.execute(
                """SELECT r.reservation_id, r.visitor_name, r.qty, r.status, r.team_id,
                          t.agency_code, (c2.checkin_id IS NOT NULL) AS checked_in
                   FROM reservations r
                   LEFT JOIN teams t ON r.team_id=t.team_id
                   LEFT JOIN checkins c2 ON c2.reservation_id=r.reservation_id
                   WHERE r.session_id=?
                   ORDER BY r.created_at""",
                (session_id,),
            ).fetchall()
            return [dict(x) for x in rows]

    # ===================================================================
    # 补偿决定
    # ===================================================================
    def decide_compensation(
        self,
        actor: Actor,
        reservation_id: str,
        kind: str,
        amount: int = 0,
        note: str = "",
    ) -> str:
        """调度员对无法转场的游客做补偿决定（S14）。"""
        self._require_dispatcher(actor, "补偿决定")
        if kind not in ("refund", "upgrade", "voucher"):
            raise DomainError("补偿类型必须是 refund/upgrade/voucher")
        with self.store.write() as c:
            res = self._get_reservation(c, reservation_id)
            comp_id = _new_id("comp")
            c.execute(
                """INSERT INTO compensations(comp_id, reservation_id, session_id,
                       kind, amount, note, decided_by, decided_at)
                   VALUES(?,?,?,?,?,?,?,?)""",
                (comp_id, reservation_id, res["session_id"], kind, amount, note,
                 actor.actor_id, _now()),
            )
            self._add_timeline(
                c, res["session_id"], reservation_id, actor.actor_id,
                TimelineKind.COMPENSATION, COMPENSATION_DECISION,
                f"补偿决定：{res['visitor_name']} → {kind} {amount/100:.2f} 元（{note}）",
                {"kind": kind, "amount": amount},
            )
            return comp_id

    # ===================================================================
    # 时间线与场次解释视图（运营可解释性）
    # ===================================================================
    def timeline(self, session_id: str | None = None) -> list[dict]:
        """同一条时间线：安全调整/取消/转场/补偿/退款/拒绝/核验，按时间排列。"""
        with self.store.read() as c:
            if session_id:
                rows = c.execute(
                    "SELECT * FROM timeline WHERE session_id=? ORDER BY event_id",
                    (session_id,),
                ).fetchall()
            else:
                rows = c.execute("SELECT * FROM timeline ORDER BY event_id").fetchall()
            return [dict(r) for r in rows]

    def explain_session(self, session_id: str) -> dict:
        """回答运营三问：每个名额为何分配、哪条规则触发限制、游客最终安排。"""
        with self.store.read() as c:
            sess = dict(self._get_session(c, session_id))
            reservations = [dict(r) for r in c.execute(
                "SELECT * FROM reservations WHERE session_id=? ORDER BY rowid",
                (session_id,),
            ).fetchall()]
            events = [dict(r) for r in c.execute(
                "SELECT * FROM timeline WHERE session_id=? ORDER BY event_id",
                (session_id,),
            ).fetchall()]

            used = self._confirmed_qty(c, session_id)
            seat_story: list[dict] = []
            for r in reservations:
                why = self._explain_reservation(c, r, events)
                seat_story.append(why)

            triggered = sorted({e["rule"] for e in events if e["rule"]})
            return {
                "session": {
                    "session_id": session_id,
                    "title": sess["title"],
                    "state": sess["state"],
                    "safe_capacity": sess["safe_capacity"],
                    "original_capacity": sess["original_capacity"],
                    "confirmed_seats": used,
                    "free_seats": sess["safe_capacity"] - used,
                },
                "seat_allocation": seat_story,
                "rules_triggered": [{"rule": x, "text": RULES[x]} for x in triggered],
                "timeline": [
                    {"ts": e["ts"], "kind": e["kind"], "rule": e["rule"],
                     "actor": e["actor_id"], "summary": e["summary"],
                     "reservation_id": e["reservation_id"],
                     "payload": Store.loads(e["payload"])}
                    for e in events
                ],
            }

    def _explain_reservation(
        self, c: sqlite3.Connection, r: dict, events: list[dict]
    ) -> dict:
        """把一条预约的生命周期串成"为何占名额 / 被哪条规则限制 / 最终安排"。"""
        related = [e for e in events if e["reservation_id"] == r["reservation_id"]]
        final = r["status"]
        target = None
        if final == ReservationStatus.REROLLED.value:
            target = c.execute(
                "SELECT session_id FROM reservations WHERE reroll_from=?",
                (r["reservation_id"],),
            ).fetchone()
        comp = c.execute(
            "SELECT kind, amount, note FROM compensations WHERE reservation_id=?",
            (r["reservation_id"],),
        ).fetchone()
        refund = c.execute(
            "SELECT status, amount, reason FROM refunds WHERE reservation_id=? ORDER BY requested_at DESC LIMIT 1",
            (r["reservation_id"],),
        ).fetchone()

        if final == ReservationStatus.CONFIRMED.value:
            why = "占名额：已确认售票"
            limiting_rule = ""
        elif final == ReservationStatus.WAITLISTED.value:
            why = "未占名额：容量/票种池不足，按 FIFO 候补"
            limiting_rule = next((e["rule"] for e in related if e["kind"] == "waitlist"), "S06")
        elif final == ReservationStatus.REROLLED.value:
            why = "原名额已释放：整团转场"
            limiting_rule = "S13"
        elif final == ReservationStatus.REFUNDED.value:
            why = "名额已释放：已退款"
            limiting_rule = next((e["rule"] for e in related if e["kind"] == "refund"), "S09")
        elif final == ReservationStatus.CANCELLED_BY_OPS.value:
            why = "名额已收回：限流/取消挤出，退款处理中或已补偿"
            limiting_rule = next((e["rule"] for e in related if e["rule"] in ("S10", "S11")), "S10")
        else:  # rejected
            late = [e for e in related if e["rule"] == REFUND_LATE_ACK_REJECTED]
            why = "终态后迟到回执被拒，场次不重开" if late else "候补随场次取消作废"
            limiting_rule = "S12" if late else "S11"

        checked = c.execute(
            "SELECT 1 FROM checkins WHERE reservation_id=?", (r["reservation_id"],)
        ).fetchone()
        return {
            "reservation_id": r["reservation_id"],
            "visitor_name": r["visitor_name"],
            "qty": r["qty"],
            "team_id": r["team_id"],
            "status": final,
            "why": why,
            "limiting_rule": limiting_rule,
            "final_arrangement": {
                "reroll_to": target["session_id"] if target else None,
                "refund": dict(refund) if refund else None,
                "compensation": dict(comp) if comp else None,
                "checked_in": bool(checked),
            },
            "events": [{"ts": e["ts"], "kind": e["kind"], "rule": e["rule"],
                        "summary": e["summary"]} for e in related],
        }

    # ===================================================================
    # 旅行社视图：只看本社团队
    # ===================================================================
    def list_team_reservations(self, actor: Actor, team_id: str) -> list[dict]:
        with self.store.read() as c:
            team = c.execute("SELECT * FROM teams WHERE team_id=?", (team_id,)).fetchone()
            if not team:
                raise DomainError(f"团队不存在：{team_id}")
            if actor.role is Role.TRAVEL_AGENCY and team["agency_code"] != actor.travel_agency_code:
                raise PolicyError(AUTH_TRAVEL_AGENCY_OWN_TEAMS, "只能查看本社团队")
            rows = c.execute(
                "SELECT * FROM reservations WHERE team_id=? ORDER BY created_at",
                (team_id,),
            ).fetchall()
            return [dict(r) for r in rows]

    # ===================================================================
    # 内部小工具
    # ===================================================================
    def _require_dispatcher(self, actor: Actor, action: str) -> None:
        if actor.role is not Role.DISPATCHER:
            raise PolicyError(AUTH_DISPATCHER_CAPACITY, f"{action}需要调度员授权")

    def _authorize_reservation_action(
        self, actor: Actor, res: sqlite3.Row, c: sqlite3.Connection, action: str
    ) -> None:
        if actor.role is Role.DISPATCHER:
            return
        if actor.role is Role.SITE_STAFF:
            raise PolicyError(AUTH_SITE_STAFF_SESSION_SCOPED, f"景区岗位不能{action}")
        if actor.role is Role.TRAVEL_AGENCY:
            if res["team_id"] is None:
                raise PolicyError(AUTH_TRAVEL_AGENCY_OWN_TEAMS, "散客预约不属于旅行社")
            team = c.execute("SELECT * FROM teams WHERE team_id=?", (res["team_id"],)).fetchone()
            if team["agency_code"] != actor.travel_agency_code:
                raise PolicyError(AUTH_TRAVEL_AGENCY_OWN_TEAMS,
                                  f"团队属于 {team['agency_code']}，不能由 {actor.travel_agency_code} 操作")
            return
        # visitor_service 只读协助，不能修改
        raise PolicyError(AUTH_TRAVEL_AGENCY_OWN_TEAMS, "该身份无权修改预约")

    @staticmethod
    def _get_session(c: sqlite3.Connection, session_id: str) -> sqlite3.Row:
        row = c.execute("SELECT * FROM sessions WHERE session_id=?", (session_id,)).fetchone()
        if not row:
            raise DomainError(f"场次不存在：{session_id}")
        return row

    @staticmethod
    def _get_reservation(c: sqlite3.Connection, rid: str) -> sqlite3.Row:
        row = c.execute("SELECT * FROM reservations WHERE reservation_id=?", (rid,)).fetchone()
        if not row:
            raise DomainError(f"预约不存在：{rid}")
        return row

    @staticmethod
    def _get_ticket_type(c: sqlite3.Connection, type_id: str, session_id: str) -> sqlite3.Row:
        row = c.execute(
            "SELECT * FROM ticket_types WHERE type_id=? AND session_id=?",
            (type_id, session_id),
        ).fetchone()
        if not row:
            raise DomainError(f"票种 {type_id} 不属于场次 {session_id} 或不存在")
        return row

    @staticmethod
    def _add_timeline(
        c: sqlite3.Connection,
        session_id: str,
        reservation_id: str | None,
        actor_id: str,
        kind: TimelineKind,
        rule: str,
        summary: str,
        payload: dict[str, Any],
    ) -> None:
        c.execute(
            """INSERT INTO timeline(ts, session_id, reservation_id, actor_id, kind,
                   rule, summary, payload)
               VALUES(?,?,?,?,?,?,?,?)""",
            (_now(), session_id, reservation_id, actor_id, kind.value, rule,
             summary, Store.dumps(payload)),
        )
