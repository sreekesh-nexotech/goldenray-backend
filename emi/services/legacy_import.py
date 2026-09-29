"""Legacy import of the EMI calculator configuration (PLAN §7.3 ``emi_*``: copy; DV-83).

``emi_bank`` → ``emi_bank``, ``emi_interest_rate_rule`` → ``emi_interest_rate_rule``, ``emi_subsidy_rule`` →
``emi_subsidy_rule``, ``emi_calculator_settings`` (the singleton row) → ``emi_settings`` and ``emi_system_size`` →
``emi_system_size`` (kept as the ``MANUAL`` price source instead of "not migrated": DV-83). Each function takes plain
row dicts as the legacy table holds them (``SELECT *``) and returns ``{"created", "updated", "skipped",
"violations"}``:

* ``core_legacy_map`` (``BACKEND`` × legacy table × id) makes re-runs update instead of duplicating; a row deleted in
  the platform since stays deleted (``skipped``); a settings row edited in Studio before the first import is kept;
* percentages become fractions (``5.75`` → ``0.0575``) and must stay exact; money must fit ``numeric(14,2)``; bank
  features must be a list of texts; nothing is invented or rounded — a refused row is skipped and reported;
* source timestamps are preserved (the settings row has only ``updated_at``, used for both); each row runs in its
  own savepoint; each call writes one audit row (``emi.legacy_imported``) and bumps ``emi:config``.

The helpers are the calculators' (``calculators.services.import_support``): both apps serve the website calculators.
"""

from __future__ import annotations

import re
from collections.abc import Iterable
from decimal import Decimal

from calculators.services.import_support import Report, Skip, exact_decimal, mapped_id, run, whole_number
from emi.models import Bank, EmiSettings, InterestRateRule, SubsidyRule, SubsidyScheme, SystemSize
from emi.models.config import HEX_COLOUR_REGEX, SLUG_REGEX
from emi.services.rows import CACHE_NAMESPACE

BANK_TABLE = "emi_bank"
INTEREST_RULE_TABLE = "emi_interest_rate_rule"
SUBSIDY_RULE_TABLE = "emi_subsidy_rule"
SETTINGS_TABLE = "emi_calculator_settings"
SYSTEM_SIZE_TABLE = "emi_system_size"
DEFAULT_LOGO_BG = "#074A4D"


def _percent(row: dict, field: str, report: Report, *, required: bool = True) -> Decimal | None:
    return exact_decimal(row, field, report, places=4, digits=6, required=required, scale=2)


def _money(row: dict, field: str, report: Report, *, required: bool = True) -> Decimal | None:
    return exact_decimal(row, field, report, places=2, digits=14, required=required)


def _kw(row: dict, field: str, report: Report, *, required: bool = True) -> Decimal | None:
    return exact_decimal(row, field, report, places=2, digits=6, required=required)


def _text(row: dict, field: str, limit: int) -> str:
    return str(row.get(field) or "")[:limit]


def _flag(row: dict, field: str, default: bool) -> bool:
    value = row.get(field)
    return default if value is None else bool(value)


def _integer(row: dict, field: str, report: Report) -> int:
    value = row.get(field)
    if value is None:
        return 0
    if isinstance(value, bool) or not isinstance(value, int):
        report.violation(row.get("id"), field, f"not a whole number: {value!r}")
        raise Skip
    return value


def _bank(row: dict, report: Report) -> dict:
    slug = _text(row, "slug", 64)
    if not re.match(SLUG_REGEX, slug):
        report.violation(row.get("id"), "slug", f"not a slug: {slug!r}")
        raise Skip
    logo_bg = _text(row, "logo_bg", 9) or DEFAULT_LOGO_BG
    if not re.match(HEX_COLOUR_REGEX, logo_bg):
        report.violation(row.get("id"), "logo_bg", f"not a hex colour: {logo_bg!r}")
        raise Skip
    features = row.get("features") or []
    if not isinstance(features, list) or not all(isinstance(item, str) and len(item) <= 255 for item in features):
        report.violation(row.get("id"), "features", "features must be a list of texts of at most 255 characters")
        raise Skip
    return {
        "name": _text(row, "name", 160),
        "abbr": _text(row, "abbr", 12),
        "slug": slug,
        "logo_bg": logo_bg,
        "annual_rate": _percent(row, "interest_rate", report),
        "min_loan": _money(row, "min_loan", report),
        "max_loan": _money(row, "max_loan", report),
        "upfront_requirement": _text(row, "upfront_requirement", 160),
        "eligibility": _text(row, "eligibility", 255),
        "cibil_required": whole_number(row, "cibil_required", report),
        "processing_fee_pct": _percent(row, "processing_fee_percent", report),
        "processing_fee_note": _text(row, "processing_fee_note", 120),
        "approval_min_days": whole_number(row, "approval_min_days", report),
        "approval_max_days": whole_number(row, "approval_max_days", report),
        "max_tenure_years": whole_number(row, "max_tenure_years", report),
        "features": list(features),
        "best_for": _text(row, "best_for", 255),
        "is_recommended": _flag(row, "is_recommended", False),
        "sort_order": whole_number(row, "sort_order", report),
        "is_active": _flag(row, "is_active", True),
    }


def _interest_rule(row: dict, report: Report) -> dict:
    return {
        "label": _text(row, "label", 120),
        "min_kw": _kw(row, "min_kw", report, required=False),
        "max_kw": _kw(row, "max_kw", report, required=False),
        "min_system_cost": _money(row, "min_cost", report, required=False),
        "max_system_cost": _money(row, "max_cost", report, required=False),
        "min_amount": _money(row, "min_loan", report, required=False),
        "max_amount": _money(row, "max_loan", report, required=False),
        "annual_rate": _percent(row, "rate", report),
        "min_annual_rate": _percent(row, "min_rate", report),
        "is_locked": _flag(row, "is_locked", False),
        "priority": _integer(row, "priority", report),
        "is_active": _flag(row, "is_active", True),
    }


def _subsidy_rule(row: dict, report: Report) -> dict:
    return {
        "scheme": SubsidyScheme.PM_SURYA_GHAR,
        "label": _text(row, "label", 120),
        "kw_from": _kw(row, "min_kw", report, required=False),
        "kw_to": _kw(row, "max_kw", report, required=False),
        "amount": _money(row, "amount", report),
        "priority": _integer(row, "priority", report),
        "is_active": _flag(row, "is_active", True),
    }


def _system_size(row: dict, report: Report) -> dict:
    capacity = _kw(row, "capacity_kw", report)
    price = _money(row, "price_per_kw", report)
    if capacity <= 0 or price <= 0:
        report.violation(row.get("id"), "capacity_kw" if capacity <= 0 else "price_per_kw", "must be greater than zero")
        raise Skip
    return {
        "label": _text(row, "label", 32),
        "capacity_kw": capacity,
        "price_per_kw": price,
        "price_min": _money(row, "price_min", report, required=False),
        "price_max": _money(row, "price_max", report, required=False),
        "monthly_bill_reference": _money(row, "monthly_bill_reference", report),
        "sort_order": whole_number(row, "sort_order", report),
        "is_active": _flag(row, "is_active", True),
    }


def _settings(row: dict, report: Report) -> dict | None:
    """The singleton row; a platform settings row that did not come from the import (a Studio edit) is kept."""
    if mapped_id(SETTINGS_TABLE, row["id"]) is None and EmiSettings.objects.exists():
        report.violation(row.get("id"), "row", "the platform already has its own EMI settings (edited in Studio); the legacy row was not applied")
        report.skipped += 1
        return None
    quick_adds = row.get("down_payment_quick_adds") or []
    if not isinstance(quick_adds, list):
        report.violation(row.get("id"), "down_payment_quick_adds", "must be a list of amounts")
        raise Skip
    amounts = [exact_decimal({"id": row.get("id"), "down_payment_quick_adds": amount}, "down_payment_quick_adds", report, places=2, digits=14) for amount in quick_adds]
    return {
        "tenure_min_years": whole_number(row, "tenure_min_years", report, minimum=1),
        "tenure_max_years": whole_number(row, "tenure_max_years", report, minimum=1),
        "tenure_default_years": whole_number(row, "tenure_default_years", report, minimum=1),
        "daily_saving_divisor": whole_number(row, "daily_saving_divisor", report, minimum=1),
        "price_step": _money(row, "price_step", report),
        "down_payment_min_pct": _percent(row, "down_payment_min_percent", report),
        "down_payment_max_pct": _percent(row, "down_payment_max_percent", report),
        "down_payment_step_pct": _percent(row, "down_payment_step_percent", report),
        "down_payment_quick_adds": amounts,
        "rate_max": _percent(row, "rate_max", report),
        "default_annual_rate": _percent(row, "default_interest_rate", report),
        "panel_life_years": whole_number(row, "panel_life_years", report),
    }


def _run(rows: Iterable[dict], table: str, model, transform, *, user) -> dict:
    return run(rows, table, model, transform, user=user, app="emi", namespace=CACHE_NAMESPACE)


def import_banks(rows: Iterable[dict], *, user=None) -> dict:
    return _run(rows, BANK_TABLE, Bank, _bank, user=user)


def import_interest_rules(rows: Iterable[dict], *, user=None) -> dict:
    return _run(rows, INTEREST_RULE_TABLE, InterestRateRule, _interest_rule, user=user)


def import_subsidy_rules(rows: Iterable[dict], *, user=None) -> dict:
    return _run(rows, SUBSIDY_RULE_TABLE, SubsidyRule, _subsidy_rule, user=user)


def import_settings(rows: Iterable[dict], *, user=None) -> dict:
    return _run(rows, SETTINGS_TABLE, EmiSettings, _settings, user=user)


def import_system_sizes(rows: Iterable[dict], *, user=None) -> dict:
    return _run(rows, SYSTEM_SIZE_TABLE, SystemSize, _system_size, user=user)


def import_all(
    *, banks: Iterable[dict] = (), interest_rules: Iterable[dict] = (), subsidy_rules: Iterable[dict] = (), settings: Iterable[dict] = (), system_sizes: Iterable[dict] = (), user=None
) -> dict:
    """Every EMI table, keyed by legacy table name (``migrations_tools`` passes the ``SELECT *`` rows)."""
    return {
        SETTINGS_TABLE: import_settings(settings, user=user),
        BANK_TABLE: import_banks(banks, user=user),
        INTEREST_RULE_TABLE: import_interest_rules(interest_rules, user=user),
        SUBSIDY_RULE_TABLE: import_subsidy_rules(subsidy_rules, user=user),
        SYSTEM_SIZE_TABLE: import_system_sizes(system_sizes, user=user),
    }
