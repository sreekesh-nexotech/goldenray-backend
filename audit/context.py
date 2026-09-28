"""Per-request audit context: request id, client IP and the acting principal (``contextvars``).

* ``audit.middleware.AuditMiddleware`` opens a fresh context for every HTTP request (request id + client IP) and
  closes it when the response is returned.
* The authentication classes attach the actor once DRF has authenticated the request (:func:`set_actor`) — staff
  JWT (``accounts.authentication``) and service tokens (``core.service_credentials``).
* Celery tasks and management commands may :func:`bind` a context explicitly; otherwise the context is empty and
  ``audit.services.record`` attributes rows to ``SYSTEM`` unless the caller names an actor.

The context never holds request bodies — only identifiers.
"""

from __future__ import annotations

from collections.abc import Iterator
from contextlib import contextmanager
from contextvars import ContextVar, Token
from dataclasses import dataclass


@dataclass
class AuditContext:
    request_id: str | None = None
    ip: str | None = None
    actor: object | None = None
    actor_kind: str | None = None


_context: ContextVar[AuditContext | None] = ContextVar("flarize_audit_context", default=None)


def current() -> AuditContext:
    """The active context, or an empty one outside a request/bound block (never ``None``)."""
    ctx = _context.get()
    return ctx if ctx is not None else AuditContext()


def begin(*, request_id: str | None = None, ip: str | None = None, actor=None, actor_kind: str | None = None) -> Token:
    """Open a new context; pass the returned token to :func:`end`."""
    return _context.set(AuditContext(request_id=request_id, ip=ip or None, actor=actor, actor_kind=actor_kind))


def end(token: Token) -> None:
    _context.reset(token)


def set_actor(actor, actor_kind: str | None) -> None:
    """Attach the authenticated principal to the open context (no-op outside one)."""
    ctx = _context.get()
    if ctx is not None:
        ctx.actor = actor
        ctx.actor_kind = actor_kind


@contextmanager
def bind(*, request_id: str | None = None, ip: str | None = None, actor=None, actor_kind: str | None = None) -> Iterator[AuditContext]:
    """Run a block (Celery task, management command) with an explicit audit context."""
    token = begin(request_id=request_id, ip=ip, actor=actor, actor_kind=actor_kind)
    try:
        yield current()
    finally:
        end(token)
