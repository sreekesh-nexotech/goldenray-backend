"""Component status lifecycle (PLAN §2.2, §3.4) and the selection rule other contexts call.

::

    DRAFT ──activate──▶ ACTIVE ──deprecate(replacement?, reason)──▶ DEPRECATED
      │                   ▲  │                                          │
      │                   └──┼──────────────activate────────────────────┘
      └──────retire──────────┴──retire──▶ RETIRED  (terminal)  ◀──retire──┘

* ``activate`` needs the spec for panels and inverters (the BOM builder sizes by wattage / kW) and an active
  category; re-activating a DEPRECATED component clears its reason and replacement;
* ``deprecate`` needs a reason; the optional replacement must be a live, non-retired component of the same
  category, not the component itself, and must not lead back to it (no replacement cycles);
* ``retire`` is terminal: a RETIRED component stays referenced by history but can never be selected again.

:func:`assert_selectable` is the rule packs, BOM and quotations call before putting a component in anything new
(PLAN: "RETIRED cannot be selected in a new pack or quotation", PBC-M-007 re-checks it at publish).
"""

from __future__ import annotations

from django.db import transaction
from django.utils import timezone

from audit.services import record
from catalog.models import Component, ComponentStatus
from catalog.services.common import CACHE_NAMESPACE, validation_error
from catalog.services.history import record_change
from catalog.services.specs import REQUIRED_FOR_ACTIVE, get_spec, spec_kind
from core.errors import Conflict, DomainError
from core.outbox import emit
from core.services import check_version
from flarize.cache_utils import bump

MAX_REPLACEMENT_CHAIN = 50
TRANSITIONS = {
    "activate": ({ComponentStatus.DRAFT, ComponentStatus.DEPRECATED}, ComponentStatus.ACTIVE),
    "deprecate": ({ComponentStatus.ACTIVE}, ComponentStatus.DEPRECATED),
    "retire": ({ComponentStatus.DRAFT, ComponentStatus.ACTIVE, ComponentStatus.DEPRECATED}, ComponentStatus.RETIRED),
}


class ComponentNotSelectable(DomainError):
    status = 400
    default_code = "component_not_selectable"


def assert_selectable(component: Component, *, field: str = "component") -> Component:
    """Raise unless ``component`` may be put into a new pack, BOM or quotation.

    RETIRED → 400 ``component_retired`` (the message names the replacement when there is one); soft-deleted →
    400 ``component_deleted``. DRAFT, ACTIVE and DEPRECATED pass (DEPRECATED is a warning the caller may surface
    via :func:`selection_warning`). Returns the component for chaining.
    """
    if component is None:
        raise ComponentNotSelectable("component_not_found", "The component does not exist.", errors={field: ["Unknown component."]})
    if component.deleted_at is not None:
        raise ComponentNotSelectable("component_deleted", f"Component {component.sku} has been deleted.", errors={field: ["The component has been deleted."]})
    if component.status == ComponentStatus.RETIRED:
        replacement = _usable_replacement(component)
        hint = f" Use {replacement.sku} instead." if replacement is not None else ""
        raise ComponentNotSelectable("component_retired", f"Component {component.sku} is retired and cannot be selected.{hint}", errors={field: [f"{component.sku} is retired."]})
    return component


def _usable_replacement(component: Component) -> Component | None:
    """The suggested replacement, unless it was deleted or retired since (it would not be selectable either)."""
    replacement = component.replacement if component.replacement_id else None
    if replacement is None or replacement.deleted_at is not None or replacement.status == ComponentStatus.RETIRED:
        return None
    return replacement


def selection_warning(component: Component) -> str | None:
    """A human warning for DEPRECATED components (with the suggested replacement), else ``None``."""
    if component.status != ComponentStatus.DEPRECATED:
        return None
    replacement = _usable_replacement(component)
    suffix = f" Suggested replacement: {replacement.sku}." if replacement is not None else ""
    return f"{component.sku} is deprecated ({component.deprecated_reason}).{suffix}"


def _locked(instance: Component) -> Component:
    return Component.objects.select_for_update(of=("self",)).select_related("category", "replacement").get(pk=instance.pk)


def _check_transition(component: Component, action: str) -> str:
    allowed, target = TRANSITIONS[action]
    if component.status not in allowed:
        raise Conflict("invalid_status_transition", f"Cannot {action} a {component.status} component.", errors={"status": [component.status]})
    return target


def _finish(component: Component, *, user, action: str, before_status: str, reason: str, extra: dict | None = None) -> Component:
    extra = extra or {}
    record_change(component, user=user, field="status", old=before_status, new=component.status, reason=reason)
    record(f"catalog.component_{action}d", obj=component, actor=user, before={"status": before_status}, after={"status": component.status, **extra}, note=reason)
    bump(CACHE_NAMESPACE)
    emit(
        "catalog.component_status_changed",
        {"component_uid": str(component.uid), "sku": component.sku, "from": before_status, "to": component.status, **extra},
        aggregate_type="catalog.component",
        aggregate_uid=component.uid,
    )
    return component


@transaction.atomic
def activate(instance: Component, *, user, expected_version=None, reason: str = "") -> Component:
    component = _locked(instance)
    check_version(component, expected_version)
    target = _check_transition(component, "activate")
    if not component.category.is_active or component.category.deleted_at is not None:
        raise Conflict("category_inactive", "The component's category is not active.")
    kind = spec_kind(component.category)
    if kind in REQUIRED_FOR_ACTIVE and get_spec(component, kind) is None:
        raise Conflict("spec_required", f"Add the {kind} spec before activating the component.", errors={f"{kind}_spec": ["Required to activate."]})
    before = component.status
    component.versioned_update(user, status=target, status_changed_at=timezone.now(), deprecated_reason="", replacement=None)
    return _finish(component, user=user, action="activate", before_status=before, reason=reason)


def _check_replacement(component: Component, replacement: Component | None) -> None:
    if replacement is None:
        return
    if replacement.pk == component.pk:
        raise validation_error({"replacement_uid": ["A component cannot replace itself."]})
    if replacement.deleted_at is not None or replacement.status == ComponentStatus.RETIRED:
        raise validation_error({"replacement_uid": ["The replacement must be a live component that is not retired."]})
    if replacement.category_id != component.category_id:
        raise validation_error({"replacement_uid": ["The replacement must belong to the same category."]})
    seen, current = {component.pk}, replacement
    for _ in range(MAX_REPLACEMENT_CHAIN):
        if current.replacement_id is None:
            return
        if current.replacement_id in seen:
            raise validation_error({"replacement_uid": ["This replacement would create a replacement cycle."]})
        seen.add(current.pk)
        current = Component.all_objects.get(pk=current.replacement_id)
    raise validation_error({"replacement_uid": ["The replacement chain is too long."]})


@transaction.atomic
def deprecate(instance: Component, *, user, reason: str, replacement: Component | None = None, expected_version=None) -> Component:
    component = _locked(instance)
    check_version(component, expected_version)
    target = _check_transition(component, "deprecate")
    reason = (reason or "").strip()
    if not reason:
        raise validation_error({"reason": ["A reason is required."]})
    _check_replacement(component, replacement)
    before = component.status
    component.versioned_update(user, status=target, status_changed_at=timezone.now(), deprecated_reason=reason, replacement=replacement)
    extra = {"replacement_uid": str(replacement.uid) if replacement else None}
    if replacement is not None:
        record_change(component, user=user, field="replacement", old=None, new=replacement.sku, reason=reason)
    return _finish(component, user=user, action="deprecate", before_status=before, reason=reason, extra=extra)


@transaction.atomic
def retire(instance: Component, *, user, reason: str = "", expected_version=None) -> Component:
    component = _locked(instance)
    check_version(component, expected_version)
    target = _check_transition(component, "retire")
    reason = (reason or "").strip()
    before = component.status
    component.versioned_update(user, status=target, status_changed_at=timezone.now(), retired_reason=reason)
    return _finish(component, user=user, action="retire", before_status=before, reason=reason)
