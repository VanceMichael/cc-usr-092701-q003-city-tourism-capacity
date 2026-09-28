"""参与方与授权模型。

角色（对应领域参与方）：

- ``dispatcher``    文旅调度员：安全调整、取消、转场、补偿、跨区域调配、
  免费名额授权，以及全部只读视图（含时间线与名额解释）。
- ``venue_staff``   景区工作人员：只能读取本景区场次的名单并做现场核验。
- ``agency``        旅行社：只能创建与修改本社的团队预约。
- ``visitor_service`` 游客服务人员：售票、退款、候补等对客操作。
"""

from __future__ import annotations

from dataclasses import dataclass

from .errors import PermissionDeniedError

DISPATCHER = "dispatcher"
VENUE_STAFF = "venue_staff"
AGENCY = "agency"
VISITOR_SERVICE = "visitor_service"

ROLES = frozenset({DISPATCHER, VENUE_STAFF, AGENCY, VISITOR_SERVICE})

# 权限点
P_VENUE_MANAGE = "venue.manage"
P_ACTIVITY_MANAGE = "activity.manage"
P_SAFETY_ADJUST = "safety.adjust"
P_SESSION_CANCEL = "session.cancel"
P_SESSION_TRANSFER = "session.transfer"
P_COMPENSATION_DECIDE = "compensation.decide"
P_FREE_QUOTA = "free_quota.issue"
P_CROSS_REGION = "cross_region.assign"
P_SELL = "booking.sell"
P_REFUND = "booking.refund"
P_WAITLIST = "booking.waitlist"
P_TEAM_MANAGE = "team.manage"
P_ROSTER_READ = "roster.read"
P_CHECKIN = "checkin.perform"
P_TIMELINE_READ = "timeline.read"
P_VIEW_ALL = "view.all"

_ROLE_PERMS: dict[str, frozenset[str]] = {
    DISPATCHER: frozenset(
        {
            P_VENUE_MANAGE,
            P_ACTIVITY_MANAGE,
            P_SAFETY_ADJUST,
            P_SESSION_CANCEL,
            P_SESSION_TRANSFER,
            P_COMPENSATION_DECIDE,
            P_FREE_QUOTA,
            P_CROSS_REGION,
            P_SELL,
            P_REFUND,
            P_WAITLIST,
            P_TEAM_MANAGE,
            P_ROSTER_READ,
            P_CHECKIN,
            P_TIMELINE_READ,
            P_VIEW_ALL,
        }
    ),
    VENUE_STAFF: frozenset({P_ROSTER_READ, P_CHECKIN}),
    AGENCY: frozenset(
        {P_TEAM_MANAGE, P_SELL, P_REFUND, P_WAITLIST, P_SESSION_TRANSFER}
    ),
    VISITOR_SERVICE: frozenset({P_SELL, P_REFUND, P_WAITLIST}),
}


@dataclass(frozen=True)
class Actor:
    """请求主体。

    agency_id 仅旅行社角色使用，用于资源归属隔离；
    venue_id 仅景区工作人员使用，限定其可接触的场地/名单。
    """

    role: str
    name: str
    agency_id: str | None = None
    venue_id: str | None = None

    def __post_init__(self) -> None:
        if self.role not in ROLES:
            raise PermissionDeniedError(f"未知角色：{self.role}")
        if self.role == AGENCY and not self.agency_id:
            raise PermissionDeniedError("旅行社主体必须携带 agency_id")
        if self.role == VENUE_STAFF and not self.venue_id:
            raise PermissionDeniedError("景区工作人员必须携带 venue_id")

    def can(self, perm: str) -> bool:
        return perm in _ROLE_PERMS[self.role]

    def require(self, perm: str) -> None:
        if not self.can(perm):
            raise PermissionDeniedError(
                f"角色 {self.role} 无权执行：{perm}"
            )

    def require_agency(self, owner_agency_id: str | None) -> None:
        """旅行社只能操作本社资源；调度员不受限；游客服务人员可处理
        无归属的散客单。"""
        if self.role == DISPATCHER:
            return
        if self.role == AGENCY:
            if owner_agency_id is not None and self.agency_id == owner_agency_id:
                return
            raise PermissionDeniedError("只能修改本旅行社的团队")
        return  # visitor_service 等其它角色由外层权限点控制

    def require_venue(self, session_venue_id: str) -> None:
        """景区工作人员只能接触本场次（其所属场地）的名单。"""
        if self.role == DISPATCHER:
            return
        if self.role == VENUE_STAFF and self.venue_id == session_venue_id:
            return
        raise PermissionDeniedError("景区工作人员只能查看本景区场次的名单")
