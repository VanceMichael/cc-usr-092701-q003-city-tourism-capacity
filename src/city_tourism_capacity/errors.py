"""领域错误类型。

所有面向调用方的失败都使用这里的异常，HTTP 层据此映射状态码。
"""

from __future__ import annotations


class DomainError(Exception):
    """业务错误基类。"""

    code = "domain_error"
    http_status = 400


class NotFoundError(DomainError):
    code = "not_found"
    http_status = 404


class ConflictError(DomainError):
    """并发冲突或当前状态不允许该操作。"""

    code = "conflict"
    http_status = 409


class CapacityError(ConflictError):
    """容量或安全上限无法满足。"""

    code = "capacity_exceeded"


class TeamAtomicError(ConflictError):
    """团队必须整体处理，剩余名额不足以容纳整个团队。"""

    code = "team_cannot_split"


class PermissionDeniedError(DomainError):
    code = "permission_denied"
    http_status = 403


class ValidationError(DomainError):
    code = "validation_error"
    http_status = 422


class SessionClosedError(ConflictError):
    """场次已结束/已取消，迟到回执不得重开。"""

    code = "session_closed"
