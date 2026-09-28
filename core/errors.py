"""Domain errors raised by services. The global exception handler renders ``{code, message, errors, error_codes}``.

``code`` is a stable machine string documented in OpenAPI; ``message`` is for humans; ``errors`` maps fields to
messages when the failure concerns specific input.
"""

from __future__ import annotations


class DomainError(Exception):
    status = 400
    default_code = "domain_error"
    default_message = "The request could not be completed."

    def __init__(self, code: str | None = None, message: str | None = None, status: int | None = None, errors: dict | None = None):
        self.code = code or self.default_code
        self.message = message or self.default_message
        if status is not None:
            self.status = status
        self.errors = errors or {}
        super().__init__(self.message)

    def __repr__(self) -> str:
        return f"{self.__class__.__name__}(code={self.code!r}, status={self.status}, message={self.message!r})"


class NotFound(DomainError):
    status = 404
    default_code = "not_found"
    default_message = "Not found."


class Conflict(DomainError):
    status = 409
    default_code = "conflict"
    default_message = "The request conflicts with the current state of the resource."


class PermissionDenied(DomainError):
    status = 403
    default_code = "permission_denied"
    default_message = "You do not have permission to perform this action."


class StaleVersion(Conflict):
    default_code = "stale_version"
    default_message = "This record was changed by someone else. Reload it and try again."
