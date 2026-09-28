"""领域枚举与少量值对象。持久化结构在 store.py，业务规则在 rules.py。"""

from __future__ import annotations

import dataclasses
import enum


class Role(enum.Enum):
    DISPATCHER = "dispatcher"        # 文旅值班调度员
    SITE_STAFF = "site_staff"        # 景区工作人员（场次岗）
    TRAVEL_AGENCY = "travel_agency"  # 旅行社（本社团队）
    VISITOR_SERVICE = "visitor_service"  # 游客服务人员（只读协助）


class SessionState(enum.Enum):
    SCHEDULED = "scheduled"
    FINISHED = "finished"      # 正常结束（终态）
    CANCELLED = "cancelled"    # 取消（终态）


class TicketKind(enum.Enum):
    PAID = "paid"      # 售票
    FREE = "free"      # 免费名额（授权签发）


class ReservationStatus(enum.Enum):
    CONFIRMED = "confirmed"  # 已确认、占名额
    WAITLISTED = "waitlisted"  # 候补中
    REROLLED = "rerolled"    # 已转场（原预约不再占名额）
    REFUNDED = "refunded"    # 已退款
    CANCELLED_BY_OPS = "cancelled_by_ops"  # 场次取消/限流挤出
    REJECTED = "rejected"    # 终态后迟到回执等被拒


class RefundStatus(enum.Enum):
    PENDING = "pending"    # 待处理（崩溃恢复依据）
    DONE = "done"          # 已完成
    REJECTED = "rejected"  # 被规则拒绝（如终态后迟到回执）


class TimelineKind(enum.Enum):
    CAPACITY_ADJUST = "capacity_adjust"
    CANCEL = "cancel"
    REROLL = "reroll"
    COMPENSATION = "compensation"
    REFUND = "refund"
    REJECTED_ACK = "rejected_ack"
    AUTH_GRANT = "auth_grant"
    SALE = "sale"
    WAITLIST = "waitlist"
    CHECKIN = "checkin"
    FINISH = "finish"


@dataclasses.dataclass(frozen=True)
class Actor:
    """调用方身份。travel_agency_code 仅旅行社有；staff_session_id 是岗所属场次。"""

    actor_id: str
    role: Role
    travel_agency_code: str | None = None
    staff_session_id: str | None = None

    @staticmethod
    def dispatcher(actor_id: str) -> "Actor":
        return Actor(actor_id, Role.DISPATCHER)

    @staticmethod
    def site_staff(actor_id: str, session_id: str) -> "Actor":
        return Actor(actor_id, Role.SITE_STAFF, staff_session_id=session_id)

    @staticmethod
    def agency(actor_id: str, agency_code: str) -> "Actor":
        return Actor(actor_id, Role.TRAVEL_AGENCY, travel_agency_code=agency_code)

    @staticmethod
    def visitor_service(actor_id: str) -> "Actor":
        return Actor(actor_id, Role.VISITOR_SERVICE)
