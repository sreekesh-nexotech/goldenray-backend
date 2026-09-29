"""Flarize's capability matrix (``src/lib/rbac.js``, ``rbac.1``) as the default policy of the commercial lifecycles.

The platform authorises through ``accounts.registry`` before a service calls an engine. The Flarize lifecycle
functions (pack config draft/submit/approve, package approval) still check a capability of their own, exactly as the
JS does; a service passes ``authorize=allow_all`` once the registry check has passed, or keeps this default policy
when it replays Flarize data (importers, parity runs).
"""

from __future__ import annotations

from typing import Any, Callable

from engines._jscompat import JsError, is_nullish, js_str, truthy

RBAC_VERSION = "rbac.1"

ROLE = {
    "ADMIN": "ADMIN",
    "PROJECT_HEAD": "PROJECT_HEAD",
    "ENGINEERING": "ENGINEERING",
    "PROCUREMENT": "PROCUREMENT",
    "SALES": "SALES",
    "SALES_HEAD": "SALES_HEAD",
    "SALES_CRS": "SALES_CRS",
    "FIELD_SALES": "FIELD_SALES",
}
ALL_ROLES = tuple(ROLE.values())

CAPABILITY = {
    name: name
    for name in (
        "PROJECT_CREATE",
        "PROJECT_VIEW",
        "PROJECT_CONFIGURE",
        "PACKAGE_SELECT",
        "BOM_VIEW",
        "BOM_EDIT",
        "BOM_RUN_CHECKER",
        "BOM_LOCK",
        "BOM_ACKNOWLEDGE_WARNING",
        "ENGINEERING_APPROVE",
        "COST_VIEW",
        "COST_INPUT_EDIT",
        "PRICING_VIEW",
        "DISCOUNT_REQUEST",
        "MARGIN_APPROVE",
        "COMMERCIAL_CONFIG_EDIT",
        "COMMERCIAL_HISTORY_VIEW",
        "RATE_CARD_EDIT",
        "PROCUREMENT_PRICE_EDIT",
        "QUOTATION_CREATE",
        "CMS_VIEW",
        "CMS_EDIT",
        "CMS_PUBLISH",
        "CMS_ASSET_MANAGE",
        "PACKAGE_CREATE",
        "PACKAGE_DUPLICATE",
        "PACKAGE_EDIT",
        "PACKAGE_SUBMIT",
        "PACKAGE_ARCHIVE",
        "PACKAGE_COMPARE",
        "PACKAGE_APPROVE",
    )
}

_SALES_CAPABILITIES = ("PROJECT_VIEW", "BOM_VIEW", "PRICING_VIEW", "DISCOUNT_REQUEST", "QUOTATION_CREATE")

CAPABILITY_MATRIX: dict[str, tuple[str, ...]] = {
    "ADMIN": (
        "PROJECT_VIEW",
        "BOM_VIEW",
        "BOM_RUN_CHECKER",
        "COST_VIEW",
        "PRICING_VIEW",
        "COMMERCIAL_CONFIG_EDIT",
        "COMMERCIAL_HISTORY_VIEW",
        "CMS_VIEW",
        "CMS_EDIT",
        "CMS_PUBLISH",
        "CMS_ASSET_MANAGE",
        "QUOTATION_CREATE",
        "PACKAGE_APPROVE",
        "PACKAGE_ARCHIVE",
        "PACKAGE_COMPARE",
    ),
    "PROJECT_HEAD": (
        "PROJECT_CREATE",
        "PROJECT_VIEW",
        "PROJECT_CONFIGURE",
        "PACKAGE_SELECT",
        "BOM_VIEW",
        "BOM_EDIT",
        "BOM_RUN_CHECKER",
        "BOM_LOCK",
        "BOM_ACKNOWLEDGE_WARNING",
        "COST_VIEW",
        "COST_INPUT_EDIT",
        "PRICING_VIEW",
        "COMMERCIAL_HISTORY_VIEW",
        "RATE_CARD_EDIT",
        "CMS_VIEW",
        "CMS_EDIT",
        "CMS_PUBLISH",
        "CMS_ASSET_MANAGE",
        "PACKAGE_CREATE",
        "PACKAGE_DUPLICATE",
        "PACKAGE_EDIT",
        "PACKAGE_SUBMIT",
        "PACKAGE_ARCHIVE",
        "PACKAGE_COMPARE",
    ),
    "ENGINEERING": ("PROJECT_VIEW", "BOM_VIEW", "BOM_RUN_CHECKER", "ENGINEERING_APPROVE", "PACKAGE_COMPARE"),
    "PROCUREMENT": ("PROJECT_VIEW", "BOM_VIEW", "PROCUREMENT_PRICE_EDIT", "COMMERCIAL_HISTORY_VIEW"),
    "SALES": _SALES_CAPABILITIES,
    "SALES_HEAD": _SALES_CAPABILITIES + ("MARGIN_APPROVE",),
    "SALES_CRS": _SALES_CAPABILITIES,
    "FIELD_SALES": _SALES_CAPABILITIES,
}

RBAC_ERROR = {
    "UNKNOWN_ROLE": "UNKNOWN_ROLE",
    "NO_ACTOR": "NO_ACTOR",
    "CAPABILITY_DENIED": "CAPABILITY_DENIED",
    "ROLE_NOT_OWNER": "ROLE_NOT_OWNER",
    "BOM_ROLE_IMMUTABLE_FOR_ACTOR": "BOM_ROLE_IMMUTABLE_FOR_ACTOR",
}

INDIVIDUAL_SALES_ROLES = ("SALES", "SALES_CRS", "FIELD_SALES")

Authorize = Callable[[Any, str], Any]


class RbacError(JsError):
    js_name = "RbacError"


def is_known_role(role: Any) -> bool:
    return isinstance(role, str) and role in ALL_ROLES


def can(role: Any, capability: str) -> bool:
    return is_known_role(role) and capability in CAPABILITY_MATRIX.get(role, ())


def assert_can(actor: Any, capability: str) -> bool:
    """``assertCan(actor, capability)``: raise :class:`RbacError` unless the actor's role holds the capability."""
    role = actor.get("role") if isinstance(actor, dict) else None
    role = None if is_nullish(role) else role
    if not truthy(actor) or not isinstance(actor, dict) or not truthy(actor.get("userId")) or not truthy(role):
        raise RbacError(
            "Every workspace operation requires an actor { userId, role }. Anonymous operations are not permitted.",
            RBAC_ERROR["NO_ACTOR"],
            None,
        )
    if not is_known_role(role):
        raise RbacError(f'Unknown role "{js_str(role)}".', RBAC_ERROR["UNKNOWN_ROLE"], {"role": role})
    if not can(role, capability):
        raise RbacError(f"Role {js_str(role)} may not perform {capability}.", RBAC_ERROR["CAPABILITY_DENIED"], {"role": role, "capability": capability})
    return True


def allow_all(actor: Any, capability: str) -> bool:
    """Policy for platform services that already authorised the caller through the registry."""
    return True


def is_individual_sales_role(role: Any) -> bool:
    return role in INDIVIDUAL_SALES_ROLES


def actor_label(actor: dict) -> Any:
    """``actor.userId ?? actor.role`` — who is recorded on a change."""
    user_id = actor.get("userId")
    return actor.get("role") if is_nullish(user_id) else user_id
