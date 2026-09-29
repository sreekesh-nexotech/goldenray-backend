"""Legacy import of the calculators' sizing tables (PLAN §7.3, DV-58, DV-75).

``solar_installations`` → ``calculators_capacity_size`` and ``solar_installation_new`` →
``calculators_bill_range_size``. Each function takes plain row dicts exactly as the legacy table holds them
(``SELECT *``: numbers, decimal strings or ``Decimal``) and returns ``{"created", "updated", "skipped",
"violations"}`` (``violations``: ``{"source_id", "field", "message"}``):

* every row is tracked in ``core_legacy_map`` (``BACKEND`` × legacy table × id): a re-run updates changed rows and
  never duplicates; a row deleted in the platform since stays deleted (``skipped``);
* values are never invented or rounded: a size must be a positive kW with at most three decimals, money must fit
  ``numeric(14,2)``, the interest rate (percent in the legacy) becomes a fraction and must stay exact, the property
  type must be Residential or Commercial (any case); a refused row is skipped and reported;
* source timestamps are preserved; each row runs in its own savepoint; each call writes one audit row
  (``calculators.legacy_imported``: counts + sha256 of the batch) and bumps ``calculators:sizes``.
"""

from __future__ import annotations

from collections.abc import Iterable
from decimal import Decimal

from calculators.models import BillRangeSize, CapacitySize, PropertyType
from calculators.services.import_support import Report, Skip, exact_decimal, run, whole_number
from calculators.services.sizing import CACHE_NAMESPACE

CAPACITY_TABLE = "solar_installations"
BILL_RANGE_TABLE = "solar_installation_new"


def _capacity_kw(row: dict, report: Report) -> Decimal:
    power = exact_decimal(row, "power_capacity", report, places=3, digits=7)
    if power <= 0:
        report.violation(row.get("id"), "power_capacity", "a system size must be positive")
        raise Skip
    return power


def _capacity(row: dict, report: Report) -> dict:
    return {
        "power_capacity_kw": _capacity_kw(row, report),
        "installation_days": whole_number(row, "time_to_complete", report),
        "total_cost": exact_decimal(row, "total_cost", report, places=2, digits=14),
        "total_subsidy": exact_decimal(row, "total_subsidy", report, places=2, digits=14),
        "area_required_sqft": whole_number(row, "area_required", report),
    }


def _property_type(row: dict, report: Report) -> str:
    text = row.get("type")
    for value, label in PropertyType.choices:
        if isinstance(text, str) and text.strip().lower() == label.lower():
            if text != label:
                report.violation(row.get("id"), "type", f"{text!r} imported as {label!r} (the calculators print the label)")
            return value
    report.violation(row.get("id"), "type", f"unknown property type {text!r} (Residential or Commercial)")
    raise Skip


def _bill_range(row: dict, report: Report) -> dict:
    return {
        "bill_range": whole_number(row, "bill_range", report, minimum=1),
        "property_type": _property_type(row, report),
        "power_capacity_kw": _capacity_kw(row, report),
        "installation_days_range": str(row.get("time_to_complete") or "")[:255],
        "total_cost": exact_decimal(row, "total_cost", report, places=2, digits=14),
        "total_subsidy": exact_decimal(row, "total_subsidy", report, places=2, digits=14),
        "area_required_sqft": whole_number(row, "area_required", report),
        "loan_available": str(row.get("loan_available") or "")[:255],
        "per_kw_rate": exact_decimal(row, "per_kw_rate", report, places=2, digits=14, required=False),
        "final_cost": exact_decimal(row, "final_cost", report, places=2, digits=14, required=False),
        "interest_rate": exact_decimal(row, "interest_rate", report, places=4, digits=6, required=False, scale=2),
        "inverter_price": exact_decimal(row, "inverter_price", report, places=2, digits=14, required=False),
    }


def import_capacity_sizes(rows: Iterable[dict], *, user=None) -> dict:
    """``solar_installations`` (``power_capacity``, ``time_to_complete`` days, costs, ``area_required``)."""
    return run(rows, CAPACITY_TABLE, CapacitySize, _capacity, user=user, app="calculators", namespace=CACHE_NAMESPACE)


def import_bill_range_sizes(rows: Iterable[dict], *, user=None) -> dict:
    """``solar_installation_new`` (``bill_range``, ``type``, ``interest_rate`` % → fraction, …)."""
    return run(rows, BILL_RANGE_TABLE, BillRangeSize, _bill_range, user=user, app="calculators", namespace=CACHE_NAMESPACE)


def import_all(*, solar_installations: Iterable[dict] = (), solar_installation_new: Iterable[dict] = (), user=None) -> dict:
    """Both tables, keyed by legacy table name (``migrations_tools`` calls it after the reference import)."""
    return {CAPACITY_TABLE: import_capacity_sizes(solar_installations, user=user), BILL_RANGE_TABLE: import_bill_range_sizes(solar_installation_new, user=user)}
