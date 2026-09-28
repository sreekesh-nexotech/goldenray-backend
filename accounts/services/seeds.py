"""The twelve seeded system roles (PLAN §3.2 "Seeded roles").

``seed_roles`` creates each missing role by slug and **never touches an existing one** — Admins may edit the seeded
grants afterwards and a re-run must not undo that. When the registry gains a module, a data migration grants it
to the system roles that need it (the registry is closed; it only changes with a release).

Scopes: every permitted module gets ``all`` unless the table names a narrower scope for it.
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass, field

from django.db import IntegrityError, transaction

from accounts.models import Role
from accounts.registry import SCOPE_ALL, full_access, grants_from_spec, normalise_scopes
from accounts.services.authz import SUPER_ADMIN_SLUG
from audit.services import record
from core.services import stamp_create


@dataclass(frozen=True)
class RoleSeed:
    slug: str
    name: str
    description: str
    grants: Mapping = field(default_factory=dict)  # compact spec: {"blogs": "*", "media": ["view"]}
    scopes: Mapping = field(default_factory=dict)  # only the modules narrower than "all"
    full: bool = False  # every module × every action

    def resolve(self) -> tuple[dict[str, list[str]], dict[str, str]]:
        if self.full:
            return full_access()
        permissions = grants_from_spec(self.grants)
        scopes = {module: self.scopes.get(module, SCOPE_ALL) for module in permissions}
        return permissions, normalise_scopes(scopes, permissions, strict=True)


_SALES_EXECUTIVE = {
    "dashboard": ["view"],
    "leads": ["view", "create", "edit"],
    "customers": ["view", "create", "edit"],
    "quotations": ["view", "create", "edit", "issue", "revise"],
    "agreements": ["view", "create", "edit"],
    "site_inspections": ["view", "create"],
    "emi": ["view"],
    "quotation_content": ["view"],
    "packs": ["view"],
}

ROLE_SEEDS: tuple[RoleSeed, ...] = (
    RoleSeed(SUPER_ADMIN_SLUG, "Super Admin", "Every module, every action. Only a Super Admin may change another Super Admin.", full=True),
    RoleSeed(
        "admin",
        "Admin",
        "Everything, including publishing prices and packs, offers, settings, audit and device management — except managing Super Admins.",
        full=True,
    ),
    RoleSeed(
        "content-manager",
        "Content Manager",
        "Website pages, blogs, FAQs, media, SEO, public product profiles and the career page.",
        grants={
            "dashboard": ["view"],
            "pages": "*",
            "blogs": "*",
            "faqs": "*",
            "media": "*",
            "seo": "*",
            "products_public": "*",
            "career_page": "*",
            "company": ["view"],
        },
    ),
    RoleSeed(
        "hr",
        "HR",
        "Careers, employees, HR setup, attendance and leave; device view and sync.",
        grants={
            "dashboard": ["view"],
            "job_positions": "*",
            "applications": "*",
            "departments": "*",
            "media": ["view", "create"],
            "employees": "*",
            "hr_setup": "*",
            "attendance": "*",
            "leave": "*",
            "devices": ["view", "sync"],
        },
    ),
    RoleSeed(
        "office-manager",
        "Office Manager",
        "Employees, attendance and leave of their own office.",
        grants={"employees": ["view"], "attendance": ["view", "export"], "leave": ["view", "approve"]},
        scopes={"employees": "office", "attendance": "office", "leave": "office"},
    ),
    RoleSeed(
        "staff",
        "Staff",
        "Own attendance and leave (assigned automatically when an employee is linked to a user).",
        grants={"dashboard": ["view"], "attendance": ["view"], "leave": ["view", "create"]},
        scopes={"attendance": "self", "leave": "self"},
    ),
    RoleSeed(
        "sales-executive",
        "Sales Executive",
        "Own leads, customers, quotations, agreements and site inspections.",
        grants=_SALES_EXECUTIVE,
        scopes={"leads": "owned", "customers": "owned", "quotations": "owned", "agreements": "owned", "site_inspections": "owned"},
    ),
    RoleSeed(
        "sales-head",
        "Sales Head",
        "Sales Executive on every record, plus discount approval, lead assignment, customer merge, agreement issue and inspection assignment.",
        grants={
            **_SALES_EXECUTIVE,
            "quotations": [*_SALES_EXECUTIVE["quotations"], "approve"],
            "leads": [*_SALES_EXECUTIVE["leads"], "manage"],
            "customers": [*_SALES_EXECUTIVE["customers"], "manage"],
            "agreements": [*_SALES_EXECUTIVE["agreements"], "issue", "manage"],
            "site_inspections": [*_SALES_EXECUTIVE["site_inspections"], "assign"],
        },
    ),
    RoleSeed(
        "project-head",
        "Project Head",
        "Catalog, pricing, BOM, packs, engineering, projects, quotation content and inspection approval/release.",
        grants={
            "catalog": ["view", "create", "edit", "approve"],
            "pricing": ["view", "edit"],
            "pricing_internal": ["view"],
            "market_rates": ["view", "edit"],
            "procurement": ["view"],
            "bom": "*",
            "packs": ["view", "edit", "submit"],
            "engineering": ["view", "verify"],
            "projects": "*",
            "quotation_content": ["view", "edit", "publish"],
            "quotations": ["view"],
            "site_inspections": ["view", "assign", "approve", "release"],
            "agreements": ["view"],
            "company": ["view"],
        },
    ),
    RoleSeed(
        "engineering",
        "Engineering",
        "Engineering checks and approvals; read access to catalog, BOM, packs and projects.",
        grants={
            "catalog": ["view"],
            "bom": ["view"],
            "packs": ["view"],
            "engineering": ["view", "verify", "approve"],
            "projects": ["view"],
            "site_inspections": ["view", "approve"],
        },
    ),
    RoleSeed(
        "field-engineer",
        "Field Engineer",
        "Site inspections assigned to them; photo uploads; customer look-up.",
        grants={"dashboard": ["view"], "site_inspections": ["view", "edit", "submit"], "media": ["create"], "customers": ["view"]},
        scopes={"site_inspections": "assigned"},
    ),
    RoleSeed(
        "procurement",
        "Procurement",
        "Catalog upkeep, procurement batches including commit, landed costs and stock.",
        grants={"catalog": ["view", "create", "edit"], "procurement": "*", "pricing_internal": ["view"], "inventory": ["view", "edit"]},
    ),
)

SEEDED_SLUGS: frozenset[str] = frozenset(seed.slug for seed in ROLE_SEEDS)


@transaction.atomic
def seed_roles(*, user=None) -> list[tuple[Role, bool]]:
    """Create the missing system roles (get-or-create by slug). Existing roles are returned untouched."""
    results: list[tuple[Role, bool]] = []
    for seed in ROLE_SEEDS:
        role = Role.objects.filter(slug=seed.slug).first()
        if role is not None:
            results.append((role, False))
            continue
        permissions, scopes = seed.resolve()
        role = Role(slug=seed.slug, name=seed.name, description=seed.description, is_system=True, permissions=permissions, scopes=scopes)
        stamp_create(role, user)
        try:
            with transaction.atomic():
                role.save()
        except IntegrityError:  # a concurrent seed created it first
            results.append((Role.objects.get(slug=seed.slug), False))
            continue
        record("accounts.role_seeded", obj=role, actor=user, actor_kind=None if user is not None else "SYSTEM", after={"slug": role.slug, "permissions": permissions, "scopes": scopes})
        results.append((role, True))
    return results
