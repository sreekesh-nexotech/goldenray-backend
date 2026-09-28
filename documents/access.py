"""Who may see a render job and download its PDF.

A document inherits the permission of the record it was rendered for. Each kind has a default registry permission;
the owning app refines it per ``object_type`` from its ``AppConfig.ready()`` — typically adding a record-level
visibility check that applies its own record scope::

    from documents import access
    access.register("quotations.quotationversion", module="quotations", action="view",
                    visible=lambda user, object_uid: quotation_visible_to(user, object_uid))

Resolution (default deny): the user needs ``(module, action)`` — otherwise 403 — and then must pass ``visible``;
without a registered ``visible`` only users whose record scope for the module is ``all``, or who requested the job
themselves, see it — otherwise 404 (the job's existence is not revealed).
"""

from __future__ import annotations

import uuid
from collections.abc import Callable
from dataclasses import dataclass

from core.errors import NotFound, PermissionDenied

Visible = Callable[[object, uuid.UUID], bool]


@dataclass(frozen=True)
class Rule:
    module: str
    action: str
    visible: Visible | None = None


KIND_DEFAULTS: dict[str, Rule] = {
    "QUOTATION": Rule("quotations", "view"),
    "AGREEMENT": Rule("agreements", "view"),
    "INSPECTION_REPORT": Rule("site_inspections", "view"),
    "ATTENDANCE_REPORT": Rule("attendance", "export"),
    "PUBLISH_REPORT": Rule("pricing", "view"),
}
_BY_OBJECT_TYPE: dict[str, Rule] = {}


def register(object_type: str, *, module: str, action: str = "view", visible: Visible | None = None) -> None:
    from accounts.registry import is_allowed

    if not is_allowed(module, action):
        raise ValueError(f"({module!r}, {action!r}) is not in the permission registry.")
    _BY_OBJECT_TYPE[object_type] = Rule(module, action, visible)


def unregister(object_type: str) -> None:
    _BY_OBJECT_TYPE.pop(object_type, None)


def rule_for(job) -> Rule:
    return _BY_OBJECT_TYPE.get(job.object_type) or KIND_DEFAULTS[job.kind]


def ensure_can_view(user, job) -> None:
    from accounts.services.authz import can, scope_for

    rule = rule_for(job)
    if not can(user, rule.module, rule.action):
        raise PermissionDenied()
    if rule.visible is not None:
        visible = bool(rule.visible(user, job.object_uid))
    else:
        visible = scope_for(user, rule.module) == "all" or (job.requested_by_id is not None and job.requested_by_id == getattr(user, "pk", None))
    if not visible:
        raise NotFound("not_found", "Render job not found.")
