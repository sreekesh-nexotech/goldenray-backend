"""Agreement drafts (PLAN §3.4 Agreements; Plan 2 §3.1): create from a quotation, create blank, edit, supersede, cancel.

* ``create_from_quotation`` — a PURCHASE_AGREEMENT or SALE_ORDER DRAFT pinned to an ISSUED quotation version the user
  can see (:mod:`agreements.services.pinning`): system, equipment, prices and the KSEB fee are copied, never typed;
* ``draft_from_accepted_quotation`` — the same, run by the ``quotations.accepted`` handler (idempotent);
* ``create_blank`` — a SALE_ORDER or EXTRA_STRUCTURE DRAFT without a quotation (DV-3: an Extra Structure agreement
  names the site inspection it prices as ``source_type``/``source_uid`` and carries one line per additional-work item);
* ``update_draft`` — PATCH of a DRAFT; on a quotation-derived draft only the non-pinned fields may change;
* ``supersede`` — a new DRAFT revision of an ISSUED/ACCEPTED agreement (optionally re-pinned to another issued version
  of the customer's quotations); the old one stays in force until the new one is issued;
* ``cancel`` — a DRAFT (``edit``) or an issued agreement (``agreements.manage`` too), with a reason.

Issue, render, documents, acceptance and price overrides are in :mod:`agreements.services.issuing` and
:mod:`agreements.services.acceptance`.
"""

from __future__ import annotations

import copy
import logging
from decimal import Decimal

from django.db import IntegrityError, transaction
from django.utils import timezone

from accounts.services.authz import can
from agreements.models import Agreement, AgreementKind, AgreementLine, AgreementStatus, InverterType, Language, Phase, SourceType, SystemType, Variant
from agreements.services import fees, pinning
from agreements.services.common import CACHE_NAMESPACE, MAX_MONEY, MODULE, agreement_snapshot, lock, money, require_draft
from audit.services import changes, record
from core.errors import Conflict, DomainError, PermissionDenied
from core.outbox import emit
from core.scopes import apply as apply_scope
from core.services import stamp_create
from customers.services.phones import try_normalise
from flarize.cache_utils import bump

logger = logging.getLogger("flarize.agreements")

FROM_QUOTATION_KINDS = (AgreementKind.PURCHASE_AGREEMENT, AgreementKind.SALE_ORDER)
BLANK_KINDS = (AgreementKind.SALE_ORDER, AgreementKind.EXTRA_STRUCTURE)
OPEN = (AgreementStatus.DRAFT, AgreementStatus.ISSUED, AgreementStatus.ACCEPTED)
IN_FORCE = (AgreementStatus.ISSUED, AgreementStatus.ACCEPTED)
# Editable on every DRAFT (not prices, not pinned equipment).
FREE_FIELDS = ("language", "extra_structure", "walkway_required", "ladder_required", "add_on_offer", "extra_description", "consumer_number", "registered_phone_e164", "wheeling_required")
# Editable only on a blank DRAFT (a quotation-derived one has them pinned).
TECHNICAL_FIELDS = (
    "system_type",
    "capacity_kw",
    "size_label",
    "phase",
    "variant",
    "panel_qty",
    "panel_capacity_label",
    "inverter_qty",
    "battery_qty",
    "structure_type",
    "structure_material",
)
COMPONENT_FIELDS = {"panel_uid": "panel", "inverter_uid": "inverter", "battery_uid": "battery"}
COMPONENT_ROLES = {"panel": ("MAIN_PANEL",), "inverter": ("MAIN_INVERTER",), "battery": ("BATTERY",)}
PRICE_FIELDS = ("original_price", "extra_cost")
PINNED_INPUTS = (*TECHNICAL_FIELDS, *COMPONENT_FIELDS, "structure_template_uid", *PRICE_FIELDS, "lines")
COPIED_FIELDS = (
    *FREE_FIELDS,
    *TECHNICAL_FIELDS,
    "panel",
    "panel_label",
    "panel_capacity_w",
    "panel_dcr",
    "inverter",
    "inverter_brand",
    "inverter_type",
    "battery",
    "battery_label",
    "structure_template",
    "original_price",
    "extra_cost",
    "discount",
    "final_price",
    "statutory_fee",
    "statutory_fee_label",
    "statutory_fee_amount",
    "price_override_reason",
    "legacy_quotation_ref",
)


def _invalid(field: str, message: str, code: str = "validation_error") -> DomainError:
    return DomainError(code, message, errors={field: [message]})


def _event(name: str, agreement: Agreement, **extra) -> None:
    emit(
        f"agreements.{name}",
        {"agreement_uid": str(agreement.uid), "customer_uid": str(agreement.customer.uid), "kind": agreement.kind, "number": agreement.number, **extra},
        aggregate_type="agreements.agreement",
        aggregate_uid=agreement.uid,
    )


# ── lookups ─────────────────────────────────────────────────────────────────────────────────────────────────────────


def _customer_for(user, customer_uid):
    from customers.models import Customer

    customer = apply_scope(Customer.objects.select_related("lead"), user, "customers").filter(uid=customer_uid).first()
    if customer is None:
        raise _invalid("customer_uid", "No customer you can see has this uid.")
    return customer


def _quotation_version_for(user, version_uid):
    from quotations.models import Version
    from quotations.services.scoping import visible

    queryset = Version.objects.select_related("quotation", "quotation__customer", "quotation__owner", "price_release")
    version = visible(queryset, user).filter(uid=version_uid).first() if user is not None else queryset.filter(uid=version_uid).first()
    if version is None:
        raise _invalid("quotation_version_uid", "No quotation version you can see has this uid.")
    return version


def _require_issued_version(version) -> None:
    if version.status != "ISSUED":
        raise Conflict("quotation_version_not_issued", f"Only the ISSUED version of a quotation can be agreed (this one is {version.status}).", errors={"quotation_version_uid": [version.status]})
    if version.quotation.status not in ("ISSUED", "ACCEPTED"):
        raise Conflict("quotation_closed", f"The quotation is {version.quotation.status}.", errors={"quotation_version_uid": [version.quotation.status]})


def _component(field: str, uid):
    from catalog.models import Component

    if uid is None:
        return None
    component = Component.objects.select_related("category", "brand", "panel_spec", "inverter_spec", "battery_spec").filter(uid=uid).first()
    if component is None or component.category.bom_role not in COMPONENT_ROLES[field]:
        raise _invalid(f"{field}_uid", f"No {field} component has this uid.")
    return component


def _structure_template(uid):
    from bom.models import StructureTemplate

    if uid is None:
        return None
    template = StructureTemplate.objects.filter(uid=uid).first()
    if template is None:
        raise _invalid("structure_template_uid", "No structure template has this uid.")
    return template


def _open_exists(version, kind: str) -> bool:
    return Agreement.objects.filter(quotation_version=version, kind=kind, status__in=OPEN).exists()


# ── create ──────────────────────────────────────────────────────────────────────────────────────────────────────────


def _save_new(agreement: Agreement, user) -> Agreement:
    stamp_create(agreement, user)
    try:
        with transaction.atomic():
            agreement.save()
    except IntegrityError:
        raise Conflict("agreement_exists", "An open agreement of this kind already exists for this quotation version.") from None
    return agreement


def _from_version(version, *, kind: str, language: str | None, user, owner) -> Agreement:
    if kind not in FROM_QUOTATION_KINDS:
        raise _invalid("kind", "PURCHASE_AGREEMENT or SALE_ORDER.")
    _require_issued_version(version)
    if _open_exists(version, kind):
        raise Conflict("agreement_exists", f"An open {kind} already exists for this quotation version; supersede it instead.")
    language = language or (version.language if version.language in Language.values else Language.EN)
    agreement = Agreement(kind=kind, customer=version.quotation.customer, owner=owner, quotation_version=version, language=language, **pinning.from_quotation_version(version))
    agreement = _save_new(agreement, user)
    record("agreements.created", obj=agreement, actor=user, after={**agreement_snapshot(agreement), "quotation_version": str(version.uid)})
    _event("created", agreement, quotation_version_uid=str(version.uid))
    bump(CACHE_NAMESPACE)
    return agreement


@transaction.atomic
def create_from_quotation(*, user, data: dict) -> Agreement:
    version = _quotation_version_for(user, data.get("quotation_version_uid"))
    owner = version.quotation.owner or (user if getattr(user, "pk", None) else None)
    return _from_version(version, kind=data.get("kind") or AgreementKind.PURCHASE_AGREEMENT, language=data.get("language"), user=user, owner=owner)


@transaction.atomic
def draft_from_accepted_quotation(payload: dict) -> Agreement | None:
    """``quotations.accepted`` → a DRAFT Purchase Agreement (once per quotation version; at-least-once safe)."""
    from quotations.models import Version

    version = Version.objects.select_related("quotation", "quotation__customer", "quotation__owner", "price_release").filter(uid=payload.get("version_uid")).first()
    if version is None:
        logger.warning("quotations.accepted names an unknown version", extra={"version_uid": payload.get("version_uid")})
        return None
    if Agreement.objects.filter(quotation_version=version, kind=AgreementKind.PURCHASE_AGREEMENT).exclude(status=AgreementStatus.CANCELLED).exists():
        return None
    if version.status != "ISSUED":
        logger.warning("quotations.accepted names a version that is not ISSUED", extra={"version_uid": str(version.uid), "status": version.status})
        return None
    language = payload.get("language") if payload.get("language") in Language.values else None
    try:
        with transaction.atomic():
            return _from_version(version, kind=AgreementKind.PURCHASE_AGREEMENT, language=language, user=None, owner=version.quotation.owner)
    except Conflict:
        return None


def _base_values(user, base_uid, customer) -> dict:
    """Technical values (and the base amount) copied from another agreement of the same customer."""
    from agreements.services.scoping import visible

    base = visible(Agreement.objects.all(), user).filter(uid=base_uid).first()
    if base is None or base.customer_id != customer.pk:
        raise _invalid("base_agreement_uid", "No agreement of this customer you can see has this uid.")
    copied = {
        name: getattr(base, name)
        for name in (*TECHNICAL_FIELDS, "panel", "panel_label", "panel_capacity_w", "panel_dcr", "inverter", "inverter_brand", "inverter_type", "battery", "battery_label", "structure_template")
    }
    copied["original_price"] = base.final_price
    return copied


@transaction.atomic
def create_blank(*, user, data: dict) -> Agreement:
    data = _site_facts(data)
    kind = data.get("kind")
    if kind not in BLANK_KINDS:
        raise _invalid("kind", "A blank agreement is a SALE_ORDER or an EXTRA_STRUCTURE agreement.")
    customer = _customer_for(user, data.get("customer_uid"))
    source_type, source_uid = data.get("source_type") or "", data.get("source_uid")
    if bool(source_type) != bool(source_uid):
        raise _invalid("source_uid", "source_type and source_uid go together.")
    if source_type and source_type not in SourceType.values:
        raise _invalid("source_type", "SITE_INSPECTION.")
    if kind == AgreementKind.EXTRA_STRUCTURE and not data.get("lines"):
        raise _invalid("lines", "An Extra Structure agreement needs at least one line.")
    agreement = Agreement(
        kind=kind, customer=customer, owner=user if getattr(user, "pk", None) else None, source_type=source_type, source_uid=source_uid or None, language=data.get("language") or Language.EN
    )
    if data.get("base_agreement_uid"):
        for name, value in _base_values(user, data["base_agreement_uid"], customer).items():
            setattr(agreement, name, value)
    values = _blank_values(agreement, data)
    for name, value in values.items():
        setattr(agreement, name, value)
    _reprice(agreement, values, lines=data.get("lines"))
    agreement = _save_new(agreement, user)
    if data.get("lines"):
        _replace_lines(agreement, data["lines"], user)
    record("agreements.created", obj=agreement, actor=user, after={**agreement_snapshot(agreement), "source_type": source_type, "source_uid": str(source_uid) if source_uid else None})
    _event("created", agreement, source_type=source_type or None, source_uid=str(source_uid) if source_uid else None)
    bump(CACHE_NAMESPACE)
    return agreement


# ── edit ────────────────────────────────────────────────────────────────────────────────────────────────────────────


def _blank_values(agreement: Agreement, data: dict) -> dict:
    """Validated technical/price columns of a blank agreement from the request ``data`` (only the keys present)."""
    values = {name: data[name] for name in (*FREE_FIELDS, *TECHNICAL_FIELDS) if name in data}
    for key, field in COMPONENT_FIELDS.items():
        if key in data:
            component = _component(field, data[key])
            values.update(pinning.component_values(field, component) if component is not None else _cleared(field))
    if "structure_template_uid" in data:
        values["structure_template"] = _structure_template(data["structure_template_uid"])
    for name in PRICE_FIELDS:
        if name in data:
            if name == "extra_cost" and agreement.kind == AgreementKind.EXTRA_STRUCTURE:
                raise _invalid("extra_cost", "An Extra Structure agreement's cost is the sum of its lines.")
            values[name] = money(data[name]) if data[name] is not None else (Decimal("0.00") if name == "extra_cost" else None)
    if "capacity_kw" in values and "size_label" not in values:
        values["size_label"] = pinning.size_label(values["capacity_kw"])
    if "lines" in data and agreement.kind != AgreementKind.EXTRA_STRUCTURE:
        raise _invalid("lines", "Only an Extra Structure agreement has lines.")
    for name, choices in (("system_type", SystemType), ("phase", Phase), ("variant", Variant), ("inverter_type", InverterType)):
        if name in values and values[name] not in [*choices.values, ""]:
            raise _invalid(name, f"Use one of {', '.join(choices.values)}.")
    return values


def _cleared(field: str) -> dict:
    return {
        "panel": {"panel": None, "panel_label": "", "panel_capacity_w": None, "panel_capacity_label": "", "panel_dcr": None},
        "inverter": {"inverter": None, "inverter_brand": "", "inverter_type": ""},
        "battery": {"battery": None, "battery_label": ""},
    }[field]


def _line_amount(line: dict) -> Decimal:
    return money(Decimal(str(line["quantity"])) * Decimal(str(line["unit_price"]))) or Decimal("0.00")


def _reprice(agreement: Agreement, values: dict, *, lines=None) -> None:
    """A blank agreement's derived prices: lines → extra cost (Extra Structure), final = original + extra − discount,
    and the KSEB fee of its size and phase (Sale Order)."""
    if lines is not None:
        amounts = [_line_amount(line) for line in lines]
        if any(amount > MAX_MONEY for amount in amounts) or sum(amounts, Decimal("0.00")) > MAX_MONEY:
            raise _invalid("lines", f"The lines add up to more than {MAX_MONEY}.")
        agreement.extra_cost = sum(amounts, Decimal("0.00"))
        values["extra_cost"] = agreement.extra_cost
    if agreement.original_price is not None:
        agreement.final_price = money(agreement.original_price + (agreement.extra_cost or 0) - (agreement.discount or 0))
        if agreement.final_price < 0:
            raise _invalid("original_price", "The discount exceeds the price.")
        if agreement.final_price > MAX_MONEY:
            raise _invalid("original_price", f"The price plus the extra cost exceeds {MAX_MONEY}.")
    else:
        agreement.final_price = None
    if agreement.kind == AgreementKind.SALE_ORDER and agreement.quotation_version_id is None and ({"capacity_kw", "phase"} & set(values) or agreement.pk is None):
        fee = fees.current(phase=agreement.phase, capacity_kw=agreement.capacity_kw)
        agreement.statutory_fee_id = fee.row_id if fee else None
        agreement.statutory_fee_label = fee.label if fee else ""
        agreement.statutory_fee_amount = money(fee.amount) if fee else None


def _replace_lines(agreement: Agreement, lines: list[dict], user) -> None:
    AgreementLine.objects.filter(agreement=agreement).delete()
    for index, line in enumerate(lines):
        row = AgreementLine(
            agreement=agreement,
            description=line["description"],
            quantity=line["quantity"],
            unit=line.get("unit") or "",
            unit_price=money(line["unit_price"]),
            amount=_line_amount(line),
            additional_work_item_uid=line.get("additional_work_item_uid"),
            sort_order=index,
        )
        stamp_create(row, user)
        row.save()


def _site_facts(data: dict) -> dict:
    """``registered_phone`` (any spelling) → ``registered_phone_e164``."""
    data = dict(data)
    if "registered_phone" in data:
        raw = data.pop("registered_phone")
        phone = try_normalise(raw) if raw else ""
        if phone is None:
            raise _invalid("registered_phone", "Not a valid phone number.")
        data["registered_phone_e164"] = phone
    return data


@transaction.atomic
def update_draft(instance: Agreement, *, user, data: dict, expected_version=None) -> Agreement:
    data = _site_facts(data)
    agreement = lock(instance, expected_version)
    require_draft(agreement)
    if agreement.quotation_version_id is not None:
        pinned = sorted(name for name in PINNED_INPUTS if name in data)
        if pinned:
            raise DomainError("field_pinned", "These values are pinned from the quotation.", errors={name: ["Pinned from the quotation version."] for name in pinned})
        values = {name: data[name] for name in FREE_FIELDS if name in data}
    else:
        values = _blank_values(agreement, data)
    lines = data.get("lines")
    if agreement.kind == AgreementKind.EXTRA_STRUCTURE and lines is not None and not lines:
        raise _invalid("lines", "An Extra Structure agreement needs at least one line.")
    before = agreement_snapshot(agreement)
    if agreement.quotation_version_id is None:
        probe = copy.copy(agreement)
        for name, value in values.items():
            setattr(probe, name, value)
        _reprice(probe, values, lines=lines)
        for name in ("extra_cost", "final_price", "statutory_fee_id", "statutory_fee_label", "statutory_fee_amount"):
            values[name] = getattr(probe, name)
    changed = {name: value for name, value in values.items() if getattr(agreement, name) != value}
    if lines is not None:
        _replace_lines(agreement, lines, user)
    if changed or lines is not None:
        agreement.versioned_update(user, **changed)
        after = agreement_snapshot(agreement)
        changed_before, changed_after = changes(before, after)
        record("agreements.draft_updated", obj=agreement, actor=user, before=changed_before, after={**changed_after, "fields": sorted(changed), "lines": len(lines) if lines is not None else None})
        bump(CACHE_NAMESPACE)
    return agreement


# ── supersede / cancel ──────────────────────────────────────────────────────────────────────────────────────────────


@transaction.atomic
def supersede(instance: Agreement, *, user, data: dict | None = None, expected_version=None) -> Agreement:
    data = data or {}
    agreement = lock(instance, expected_version)
    if agreement.status not in IN_FORCE:
        raise Conflict("agreement_not_in_force", f"Only an ISSUED or ACCEPTED agreement is superseded (this one is {agreement.status}).")
    if Agreement.objects.filter(supersedes=agreement).exclude(status=AgreementStatus.CANCELLED).exists():
        raise Conflict("already_superseded", "A revision of this agreement already exists.")
    draft = Agreement(
        kind=agreement.kind,
        customer=agreement.customer,
        owner=agreement.owner,
        quotation_version=agreement.quotation_version,
        source_type=agreement.source_type,
        source_uid=agreement.source_uid,
        revision=agreement.revision + 1,
        supersedes=agreement,
        **{name: getattr(agreement, name) for name in COPIED_FIELDS},
    )
    if data.get("language"):
        draft.language = data["language"]
    if data.get("quotation_version_uid"):
        version = _quotation_version_for(user, data["quotation_version_uid"])
        if agreement.kind not in FROM_QUOTATION_KINDS or version.quotation.customer_id != agreement.customer_id:
            raise _invalid("quotation_version_uid", "Re-pin only a Purchase Agreement or Sale Order to an issued quotation version of the same customer.")
        _require_issued_version(version)
        draft.quotation_version = version
        for name, value in pinning.from_quotation_version(version).items():
            setattr(draft, name, value)
    if agreement.legacy and draft.original_price is not None:
        total = draft.original_price + draft.extra_cost
        draft.discount = min(draft.discount, total)
        draft.final_price = money(total - draft.discount)
    draft = _save_new(draft, user)
    lines = list(agreement.lines.all())
    for line in lines:
        copy = AgreementLine(
            agreement=draft,
            description=line.description,
            quantity=line.quantity,
            unit=line.unit,
            unit_price=line.unit_price,
            amount=line.amount,
            additional_work_item_uid=line.additional_work_item_uid,
            sort_order=line.sort_order,
        )
        stamp_create(copy, user)
        copy.save()
    record("agreements.revision_created", obj=draft, actor=user, after={**agreement_snapshot(draft), "supersedes": str(agreement.uid)})
    _event("revision_created", draft, supersedes_uid=str(agreement.uid), version=draft.revision)
    bump(CACHE_NAMESPACE)
    return draft


@transaction.atomic
def cancel(instance: Agreement, *, user, reason: str, expected_version=None) -> Agreement:
    agreement = lock(instance, expected_version)
    if agreement.status not in OPEN:
        raise Conflict("agreement_closed", f"A {agreement.status} agreement cannot be cancelled.")
    if agreement.status != AgreementStatus.DRAFT and not can(user, MODULE, "manage"):
        raise PermissionDenied("cancel_issued_forbidden", "Cancelling an issued agreement needs the agreements manage permission.")
    reason = (reason or "").strip()
    if not reason:
        raise _invalid("reason", "Required.")
    previous = agreement.status
    agreement.versioned_update(user, status=AgreementStatus.CANCELLED, cancelled_at=timezone.now(), cancel_reason=reason)
    record("agreements.cancelled", obj=agreement, actor=user, before={"status": previous}, after={"status": agreement.status, "reason": reason})
    _event("cancelled", agreement, previous_status=previous, reason=reason)
    bump(CACHE_NAMESPACE)
    return agreement
