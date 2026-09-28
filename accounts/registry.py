"""The closed permission registry (PLAN §3.2). The only source of modules, actions and record scopes.

Roles store ``permissions`` (``{module: [actions]}``) and ``scopes`` (``{module: scope}``); both are normalised
against this registry on save, so a role can never hold a module/action/scope that does not exist here. There is
no superuser/staff bypass anywhere: an empty grant set can do nothing (default deny).

Record scopes (PLAN §3.2 "Scopes available per module"): ``customers``, ``quotations``, ``agreements``, ``leads``
→ all/owned; ``site_inspections`` → all/owned/assigned; ``employees``, ``attendance``, ``leave`` → all/office/self;
every other module → all. A permitted module without an explicit scope gets the module's *narrowest* default
(fail closed); importers and seeds pass ``"all"`` explicitly where the plan says so.
"""

from __future__ import annotations

from collections.abc import Iterable, Mapping
from dataclasses import dataclass

# Verbs, in display order.
ACTIONS: tuple[str, ...] = (
    "view",
    "create",
    "edit",
    "publish",
    "verify",
    "archive",
    "manage",
    "approve",
    "submit",
    "lock",
    "commit",
    "issue",
    "revise",
    "assign",
    "release",
    "export",
    "sync",
)

ACTION_LABELS: dict[str, str] = {
    "view": "View",
    "create": "Create",
    "edit": "Edit",
    "publish": "Publish",
    "verify": "Verify",
    "archive": "Archive",
    "manage": "Manage",
    "approve": "Approve",
    "submit": "Submit",
    "lock": "Lock",
    "commit": "Commit",
    "issue": "Issue",
    "revise": "Revise",
    "assign": "Assign",
    "release": "Release",
    "export": "Export",
    "sync": "Sync",
}

SCOPE_ALL = "all"
SCOPE_OWNED = "owned"
SCOPE_ASSIGNED = "assigned"
SCOPE_OFFICE = "office"
SCOPE_SELF = "self"
SCOPES: tuple[str, ...] = (SCOPE_ALL, SCOPE_OWNED, SCOPE_ASSIGNED, SCOPE_OFFICE, SCOPE_SELF)
SCOPE_LABELS: dict[str, str] = {
    SCOPE_ALL: "All records",
    SCOPE_OWNED: "Records they own",
    SCOPE_ASSIGNED: "Records assigned to them",
    SCOPE_OFFICE: "Records of their office",
    SCOPE_SELF: "Their own record",
}

GROUPS: dict[str, str] = {
    "GENERAL": "General",
    "PRODUCT": "Product",
    "CONFIG": "Configuration",
    "SALES": "Sales",
    "WEBSITE": "Website",
    "CAREERS": "Careers",
    "HR": "HR",
    "ADMIN": "Administration",
}

_SALES_SCOPES = (SCOPE_ALL, SCOPE_OWNED)
_INSPECTION_SCOPES = (SCOPE_ALL, SCOPE_OWNED, SCOPE_ASSIGNED)
_HR_SCOPES = (SCOPE_ALL, SCOPE_OFFICE, SCOPE_SELF)


@dataclass(frozen=True)
class ModuleSpec:
    key: str
    group: str
    label: str
    actions: tuple[str, ...]
    scopes: tuple[str, ...] = (SCOPE_ALL,)
    default_scope: str = SCOPE_ALL
    description: str = ""

    def __post_init__(self):
        unknown = [action for action in self.actions if action not in ACTIONS]
        if unknown:
            raise ValueError(f"Module {self.key!r} uses unknown actions {unknown}.")
        if self.group not in GROUPS:
            raise ValueError(f"Module {self.key!r} uses unknown group {self.group!r}.")
        if any(scope not in SCOPES for scope in self.scopes) or self.default_scope not in self.scopes:
            raise ValueError(f"Module {self.key!r} has an invalid scope configuration.")


_SPECS: tuple[ModuleSpec, ...] = (
    ModuleSpec("dashboard", "GENERAL", "Dashboard", ("view",), description="Gates /api/v1/dashboard/."),
    # PRODUCT
    ModuleSpec("catalog", "PRODUCT", "Catalog", ("view", "create", "edit", "approve", "archive"), description="approve = component status changes."),
    ModuleSpec("pricing", "PRODUCT", "Pricing", ("view", "edit", "publish"), description="edit = manual price rows, cost config, statutory fees; publish = PriceRelease."),
    ModuleSpec("pricing_internal", "PRODUCT", "Internal pricing", ("view",), description="Unlocks landed cost and margin fields."),
    ModuleSpec("market_rates", "PRODUCT", "Market rates", ("view", "edit", "publish")),
    ModuleSpec("offers", "PRODUCT", "Offers", ("view", "create", "edit", "approve", "publish", "archive")),
    ModuleSpec("procurement", "PRODUCT", "Procurement", ("view", "create", "edit", "commit")),
    ModuleSpec("inventory", "PRODUCT", "Inventory", ("view", "edit"), description="Stock ledger (flag INVENTORY_STOCK)."),
    # CONFIG
    ModuleSpec("bom", "CONFIG", "BOM", ("view", "edit")),
    ModuleSpec("packs", "CONFIG", "Packs", ("view", "edit", "submit", "approve", "publish")),
    ModuleSpec("engineering", "CONFIG", "Engineering", ("view", "verify", "approve"), description="verify = run checker; approve = acknowledge/waive."),
    # SALES
    ModuleSpec("leads", "SALES", "Leads", ("view", "create", "edit", "archive", "manage"), _SALES_SCOPES, SCOPE_OWNED, "manage = assign."),
    ModuleSpec("customers", "SALES", "Customers", ("view", "create", "edit", "archive", "manage"), _SALES_SCOPES, SCOPE_OWNED, "manage = merge."),
    ModuleSpec("quotations", "SALES", "Quotations", ("view", "create", "edit", "issue", "revise", "approve", "archive"), _SALES_SCOPES, SCOPE_OWNED, "approve = discounts."),
    ModuleSpec("quotation_content", "SALES", "Quotation content", ("view", "edit", "publish")),
    ModuleSpec("agreements", "SALES", "Agreements", ("view", "create", "edit", "issue", "manage"), _SALES_SCOPES, SCOPE_OWNED),
    ModuleSpec("site_inspections", "SALES", "Site inspections", ("view", "create", "edit", "assign", "submit", "approve", "release", "archive"), _INSPECTION_SCOPES, SCOPE_OWNED),
    ModuleSpec("projects", "SALES", "Projects", ("view", "create", "edit", "lock", "archive")),
    # WEBSITE
    ModuleSpec("pages", "WEBSITE", "Pages", ("view", "edit", "publish", "verify")),
    ModuleSpec("blogs", "WEBSITE", "Blogs", ("view", "create", "edit", "publish", "verify", "archive")),
    ModuleSpec("faqs", "WEBSITE", "FAQs", ("view", "create", "edit", "publish", "verify", "archive")),
    ModuleSpec("media", "WEBSITE", "Media", ("view", "create", "edit", "archive")),
    ModuleSpec("seo", "WEBSITE", "SEO", ("view", "edit", "publish")),
    ModuleSpec("products_public", "WEBSITE", "Public product profiles", ("view", "edit", "publish")),
    ModuleSpec("reference_data", "WEBSITE", "Reference data", ("view", "create", "edit", "archive")),
    ModuleSpec("emi", "WEBSITE", "EMI", ("view", "edit")),
    # CAREERS
    ModuleSpec("job_positions", "CAREERS", "Job positions", ("view", "create", "edit", "publish", "verify", "archive")),
    ModuleSpec("applications", "CAREERS", "Applications", ("view", "edit", "archive")),
    ModuleSpec("departments", "CAREERS", "Departments", ("view", "create", "edit", "archive")),
    ModuleSpec("career_page", "CAREERS", "Career page", ("view", "edit", "publish")),
    # HR
    ModuleSpec("employees", "HR", "Employees", ("view", "create", "edit", "archive"), _HR_SCOPES, SCOPE_SELF),
    ModuleSpec("hr_setup", "HR", "HR setup", ("view", "edit")),
    ModuleSpec("attendance", "HR", "Attendance", ("view", "edit", "export", "manage"), _HR_SCOPES, SCOPE_SELF),
    ModuleSpec("leave", "HR", "Leave", ("view", "create", "approve", "archive"), _HR_SCOPES, SCOPE_SELF),
    ModuleSpec("devices", "HR", "Devices", ("view", "create", "edit", "sync", "manage")),
    # ADMIN
    ModuleSpec("company", "ADMIN", "Company", ("view", "edit")),
    ModuleSpec("users", "ADMIN", "Users", ("view", "create", "edit", "archive", "manage")),
    ModuleSpec("roles", "ADMIN", "Roles", ("view", "create", "edit", "manage")),
    ModuleSpec("settings", "ADMIN", "Settings", ("view", "edit"), description="Feature flags and integrations."),
    ModuleSpec("audit", "ADMIN", "Audit log", ("view",)),
)

MODULES: dict[str, ModuleSpec] = {spec.key: spec for spec in _SPECS}

# Actions nobody may perform on their own record (PLAN §3.2 deny_self_action guard).
SELF_ACTION_DENIED: frozenset[tuple[str, str]] = frozenset({("attendance", "edit"), ("leave", "approve")})

# Modules with no endpoint of their own (they unlock fields or gate aggregate views).
PERMISSION_ONLY_MODULES: frozenset[str] = frozenset({"pricing_internal", "dashboard"})


class RegistryError(ValueError):
    """Raised by strict normalisation. ``errors`` maps the offending key to human-readable messages."""

    def __init__(self, errors: dict[str, list[str]]):
        self.errors = errors
        super().__init__("; ".join(f"{key}: {', '.join(messages)}" for key, messages in errors.items()))


def is_allowed(module: str, action: str) -> bool:
    spec = MODULES.get(module)
    return spec is not None and action in spec.actions


def module_actions(module: str) -> tuple[str, ...]:
    return MODULES[module].actions


def module_scopes(module: str) -> tuple[str, ...]:
    return MODULES[module].scopes


def _as_action_list(value) -> list | None:
    if isinstance(value, str):
        return [value]
    if isinstance(value, (list, tuple, set, frozenset)):
        return list(value)
    return None


def normalise_permissions(permissions: Mapping | None, *, strict: bool = False) -> dict[str, list[str]]:
    """Registry-ordered ``{module: [actions]}`` with unknown modules/actions removed (or rejected when ``strict``)."""
    if permissions is None:
        return {}
    if not isinstance(permissions, Mapping):
        if strict:
            raise RegistryError({"permissions": ["Must be an object of {module: [actions]}."]})
        return {}
    errors: dict[str, list[str]] = {}
    granted: dict[str, set[str]] = {}
    for module, raw_actions in permissions.items():
        spec = MODULES.get(module)
        if spec is None:
            errors.setdefault(str(module), []).append("Unknown module.")
            continue
        actions = _as_action_list(raw_actions)
        if actions is None:
            errors.setdefault(module, []).append("Actions must be a list.")
            continue
        for action in actions:
            if action in spec.actions:
                granted.setdefault(module, set()).add(action)
            else:
                errors.setdefault(module, []).append(f"Unknown action {action!r}.")
    if strict and errors:
        raise RegistryError(errors)
    return {spec.key: [action for action in spec.actions if action in granted[spec.key]] for spec in _SPECS if granted.get(spec.key)}


def normalise_scopes(scopes: Mapping | None, permissions: Mapping | None = None, *, strict: bool = False) -> dict[str, str]:
    """One allowed scope per permitted module (registry order).

    Modules come from ``permissions`` when given (scopes for modules without any permission are dropped), else from
    ``scopes``. A missing or invalid scope falls back to the module's narrowest default (fail closed); with
    ``strict`` an invalid scope or unknown module raises ``RegistryError`` instead.
    """
    if scopes is not None and not isinstance(scopes, Mapping):
        if strict:
            raise RegistryError({"scopes": ["Must be an object of {module: scope}."]})
        scopes = None
    scopes = scopes or {}
    errors: dict[str, list[str]] = {}
    for module, scope in scopes.items():
        spec = MODULES.get(module)
        if spec is None:
            errors.setdefault(str(module), []).append("Unknown module.")
        elif scope not in spec.scopes:
            errors.setdefault(module, []).append(f"Scope {scope!r} is not allowed (allowed: {', '.join(spec.scopes)}).")
    if strict and errors:
        raise RegistryError(errors)
    modules = normalise_permissions(permissions).keys() if permissions is not None else [module for module in scopes if module in MODULES]
    result: dict[str, str] = {}
    for spec in _SPECS:
        if spec.key not in modules:
            continue
        scope = scopes.get(spec.key)
        result[spec.key] = scope if scope in spec.scopes else spec.default_scope
    return result


def full_access() -> tuple[dict[str, list[str]], dict[str, str]]:
    """Every module × every action, scope ``all`` (the Super Admin seed)."""
    return {spec.key: list(spec.actions) for spec in _SPECS}, {spec.key: SCOPE_ALL for spec in _SPECS}


def grants_from_spec(spec: Mapping[str, Iterable[str] | str]) -> dict[str, list[str]]:
    """Expand a compact grant spec (``{"blogs": "*", "media": ["view"]}``) and normalise it strictly."""
    expanded = {}
    for module, actions in spec.items():
        if actions == "*":
            if module not in MODULES:
                raise RegistryError({str(module): ["Unknown module."]})
            expanded[module] = list(MODULES[module].actions)
        else:
            expanded[module] = actions
    return normalise_permissions(expanded, strict=True)


def as_dict() -> dict:
    """Serializable registry for ``GET roles/registry/`` and the Studio role editor."""
    return {
        "actions": [{"key": action, "label": ACTION_LABELS[action]} for action in ACTIONS],
        "scopes": [{"key": scope, "label": SCOPE_LABELS[scope]} for scope in SCOPES],
        "groups": [
            {
                "key": group,
                "label": label,
                "modules": [
                    {
                        "key": spec.key,
                        "label": spec.label,
                        "actions": list(spec.actions),
                        "scopes": list(spec.scopes),
                        "default_scope": spec.default_scope,
                        "description": spec.description,
                        "permission_only": spec.key in PERMISSION_ONLY_MODULES,
                    }
                    for spec in _SPECS
                    if spec.group == group
                ],
            }
            for group, label in GROUPS.items()
        ],
        "self_action_denied": [{"module": module, "action": action} for module, action in sorted(SELF_ACTION_DENIED)],
    }
