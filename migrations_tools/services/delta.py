"""``export_delta`` (PLAN §7.7): the writes made in the platform since the cutover, for manual replay after a rollback.

When a website source is rolled back (nginx back to the legacy containers), whatever staff or the website wrote in
the platform between the cutover and the rollback must be replayed into the legacy system by hand; the window is
hours. This exports, per target table of the source's import plan, every row created, changed or deleted in the
window: its uid, the legacy row it came from (``core_legacy_map``) or ``null`` for a platform-born row, the kind of
change and its current field values (foreign keys as the related row's uid; secrets and password hashes masked),
plus the window's audit actions as context. The file holds personal data (leads, applications): it is written
``0600`` and belongs to the operator running the rollback.
"""

from __future__ import annotations

import datetime as dt
import json
import os
from collections import Counter, defaultdict
from decimal import Decimal
from pathlib import Path

from django.apps import apps
from django.db import models
from django.db.models import Q

from audit.services.recording import is_sensitive_key
from core.models import LegacyMap

CMS_TARGETS = (
    "accounts_role",
    "accounts_user",
    "media_asset",
    "blog_collection",
    "blog_template",
    "blog_template_image_group",
    "blog_template_attribute_slot",
    "blog_author",
    "blog_category",
    "blog_tag",
    "blog_badge",
    "blog_entry",
    "blog_entry_category",
    "blog_entry_tag",
    "blog_entry_badge",
    "blog_entry_slug_history",
    "blog_content_block",
    "blog_entry_image",
    "blog_entry_attribute_value",
    "blog_entry_seo",
    "sitepages_page",
    "sitepages_page_seo",
    "sitepages_page_text_slot",
    "sitepages_page_image_slot",
    "faqs_category",
    "faqs_faq",
    "careers_department",
    "careers_job_position",
    "company_profile",
)
BACKEND_TARGETS = (
    "accounts_user",
    "reference_kseb_tariff",
    "reference_device_type",
    "reference_wattage",
    "reference_room_size",
    "reference_ev_car",
    "reference_ev_scooter",
    "reference_pincode",
    "reference_pincode_office",
    "catalog_brand",
    "catalog_category",
    "catalog_component",
    "catalog_component_tier",
    "catalog_component_public_profile",
    "pricing_price",
    "pricing_cost_config",
    "pricing_market_rate_set",
    "pricing_market_rate",
    "pricing_offer",
    "bom_template",
    "bom_slot",
    "bom_fixed_item",
    "bom_structure_template",
    "bom_structure_template_item",
    "bom_tube_weight",
    "calculators_capacity_size",
    "calculators_bill_range_size",
    "emi_bank",
    "emi_interest_rate_rule",
    "emi_subsidy_rule",
    "emi_settings",
    "emi_system_size",
    "company_profile",
    "seo_page_metadata",
    "leads_lead",
    "leads_affiliate_application",
    "leads_warranty_request",
    "leads_customer_installation",
    "careers_job_application",
    "careers_job_application_note",
    "careers_job_application_event",
)
TARGETS = {"CMS": CMS_TARGETS, "BACKEND": BACKEND_TARGETS}
MASKED = "***"


def model_for(table: str) -> type[models.Model] | None:
    for model in apps.get_models():
        if model._meta.db_table == table:
            return model
    return None


def _value(instance, field_):
    if field_.is_relation:
        related_id = getattr(instance, field_.attname)
        if related_id is None:
            return None
        related = field_.related_model
        if any(item.name == "uid" for item in related._meta.concrete_fields):
            return str(related._base_manager.filter(pk=related_id).values_list("uid", flat=True).first())
        return related_id
    value = getattr(instance, field_.attname)
    if field_.name == "password" or is_sensitive_key(field_.name):
        return MASKED if value else value
    if isinstance(value, dt.datetime | dt.date):
        return value.isoformat()
    if isinstance(value, Decimal):
        return str(value)
    return value


def _change(instance, since) -> str:
    deleted_at = getattr(instance, "deleted_at", None)
    if deleted_at is not None and deleted_at >= since:
        return "deleted"
    created_at = getattr(instance, "created_at", None)
    return "created" if created_at is not None and created_at >= since else "updated"


def export(system: str, *, since: dt.datetime, until: dt.datetime | None = None) -> dict:
    """The delta of ``system``'s target tables in ``[since, until)``."""
    tables: dict[str, list[dict]] = {}
    missing = []
    for table in TARGETS[system]:
        model = model_for(table)
        if model is None:
            missing.append(table)
            continue
        names = {field_.name for field_ in model._meta.concrete_fields}
        window = Q()
        for column in ("created_at", "updated_at", "deleted_at"):
            if column in names:
                bounded = Q(**{f"{column}__gte": since}) & (Q(**{f"{column}__lt": until}) if until else Q())
                window |= bounded
        if not window:
            continue
        rows = list(model._base_manager.filter(window).order_by("pk"))
        if not rows:
            continue
        legacy = defaultdict(list)
        for entry in LegacyMap.objects.filter(target_table=table, target_id__in=[row.pk for row in rows]):
            legacy[entry.target_id].append({"source_system": entry.source_system, "source_table": entry.source_table, "source_id": entry.source_id})
        tables[table] = [
            {
                "uid": str(getattr(row, "uid", "")) or None,
                "change": _change(row, since),
                "legacy": legacy.get(row.pk) or None,
                "fields": {field_.name: _value(row, field_) for field_ in model._meta.concrete_fields if field_.name not in ("id", "uid")},
            }
            for row in rows
        ]
    from audit.models import AuditLog

    audit = AuditLog.objects.filter(at__gte=since, **({"at__lt": until} if until else {})).exclude(action__contains="legacy_import").exclude(action="migrations_tools.batch_imported")
    return {
        "source_system": system,
        "since": since.isoformat(),
        "until": until.isoformat() if until else None,
        "tables": tables,
        "counts": {table: dict(Counter(row["change"] for row in rows)) for table, rows in tables.items()},
        "audit_actions": dict(Counter(audit.values_list("action", flat=True))),
        "unknown_tables": missing,
    }


def write(payload: dict, path: str | Path) -> Path:
    path = Path(path)
    descriptor = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(descriptor, "w", encoding="utf-8") as handle:
        json.dump(payload, handle, indent=1, ensure_ascii=False, default=str)
    return path
