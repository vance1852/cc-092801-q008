"""交易终止与权利回转服务向 API 和 CLI 暴露的稳定错误。"""

from __future__ import annotations


class ReversionError(RuntimeError):
    code = "reversion_error"
    status = 400


class NotFound(ReversionError):
    code = "not_found"
    status = 404


class Conflict(ReversionError):
    code = "conflict"
    status = 409


class Forbidden(ReversionError):
    code = "forbidden"
    status = 403


class InvalidState(ReversionError):
    code = "invalid_state"
    status = 409


class ValidationFailed(ReversionError):
    code = "validation_failed"
    status = 422
