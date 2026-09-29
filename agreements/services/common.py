"""Shared helpers of the agreements services: module name, cache namespace, money, locking, snapshots."""

from __future__ import annotations

from decimal import ROUND_HALF_UP, Decimal, InvalidOperation

from agreements.models import Agreement, AgreementStatus
from audit.services import snapshot
from core.errors import Conflict, NotFound
from core.services import check_version

MODULE = "agreements"
CACHE_NAMESPACE = "agreements"
OBJECT_TYPE = "agreements.agreement"
ACCEPTANCE_FOLDER = "agreements/acceptance"
CENT = Decimal("0.01")
# The largest amount a money column (numeric(14, 2)) holds.
MAX_MONEY = Decimal("999999999999.99")
AUDIT_FIELDS = (
    "number",
    "kind",
    "status",
    "revision",
    "language",
    "system_type",
    "capacity_kw",
    "size_label",
    "phase",
    "variant",
    "panel_label",
    "panel_capacity_w",
    "inverter_brand",
    "inverter_type",
    "battery_label",
    "structure_material",
    "extra_structure",
    "walkway_required",
    "ladder_required",
    "original_price",
    "extra_cost",
    "discount",
    "final_price",
    "statutory_fee_amount",
    "add_on_offer",
    "extra_description",
    "consumer_number",
    "wheeling_required",
)


def money(value) -> Decimal | None:
    """``value`` as a 2-decimal ``Decimal`` (half-up), ``None`` for empty/invalid input."""
    if value in (None, ""):
        return None
    try:
        number = Decimal(str(value).replace(",", "").strip())
    except (InvalidOperation, ValueError):
        return None
    if not number.is_finite():
        return None
    return number.quantize(CENT, rounding=ROUND_HALF_UP)


def text(value) -> str | None:
    """Decimal → plain string (``229000.00``), ``None`` stays ``None`` (JSON-safe values for payloads and events)."""
    return None if value is None else str(value)


def agreement_snapshot(agreement: Agreement) -> dict:
    return snapshot(agreement, AUDIT_FIELDS)


def agreements_queryset():
    return Agreement.objects.select_related(
        "customer", "customer__lead", "owner", "quotation_version", "quotation_version__quotation", "supersedes", "issued_by", "document_job", "statutory_fee", "acceptance_asset"
    )


def lock(agreement: Agreement, expected_version=None) -> Agreement:
    locked = Agreement.objects.select_for_update().filter(pk=agreement.pk).first()
    if locked is None:
        raise NotFound("not_found", "Agreement not found.")
    check_version(locked, expected_version)
    return locked


def require_draft(agreement: Agreement) -> None:
    if agreement.status != AgreementStatus.DRAFT:
        raise Conflict("agreement_not_draft", f"Only a DRAFT agreement can be changed (this one is {agreement.status}).", errors={"status": [agreement.status]})


def reload(agreement: Agreement) -> Agreement:
    return agreements_queryset().prefetch_related("lines").get(pk=agreement.pk)
