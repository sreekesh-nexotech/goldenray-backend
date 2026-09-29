"""The agreement document (frozen at issue) and the ``agreements.issued`` / ``agreements.superseded`` event payload.

The document is data only — the templates (``agreements/templates/documents/agreement/{en,ml,hi}.html``, ported from
the Purchase Agreement page's ``buildDoc``) format it. What the page computed in JavaScript is computed here once:

* hybrid detection — ``/hybrid/i.test(kw) || invtype === 'Hybrid Inverter'`` (or the agreement's system type);
* the plant description — ``kw + ' (phase)' + ' On-Grid|Hybrid Solar Power Plant'`` unless the size label already
  says on-grid/hybrid;
* the letterhead, the payee (``M/s <legal name>``) and the bank details — from ``company_profile`` and the primary
  ``company_bank_account`` (the page hard-coded Golden Ray's), never from the request.
"""

from __future__ import annotations

import re

from agreements.models import Agreement, AgreementKind, InverterType, SystemType
from agreements.services.common import text
from agreements.services.pinning import PHASE_LABELS

SCHEMA = "agreements.document/1"
HYBRID_RE = re.compile(r"hybrid", re.IGNORECASE)
ONGRID_OR_HYBRID_RE = re.compile(r"on-grid|hybrid", re.IGNORECASE)
YES_NO = {True: "YES", False: "NO"}


def is_hybrid(agreement: Agreement) -> bool:
    return agreement.system_type == SystemType.HYBRID or bool(HYBRID_RE.search(agreement.size_label or "")) or agreement.inverter_type == InverterType.HYBRID


def plant_description(agreement: Agreement) -> str:
    size = agreement.size_label or ""
    phase = PHASE_LABELS.get(agreement.phase, "")
    suffix = f" ({phase})" if phase else ""
    if ONGRID_OR_HYBRID_RE.search(size):
        return f"{size}{suffix}"
    label = "Hybrid Solar Power Plant" if is_hybrid(agreement) else "On-Grid Solar Power Plant"
    return f"{size}{suffix} {label}"


def company_block() -> dict:
    from company.services.bank_accounts import primary_account
    from company.services.profile import current_profile

    profile = current_profile()
    bank = primary_account()
    logo = profile.logo if profile.logo_id else None
    address = ", ".join(part for part in (profile.address_line, profile.address_locality, profile.address_region, profile.postal_code) if part)
    phone = profile.phone_e164[3:] if profile.phone_e164.startswith("+91") else profile.phone_e164
    legal = profile.legal_name or profile.trade_name
    return {
        "name": profile.display_name,
        "legal_name": legal,
        "payee": f"M/s {legal}" if legal else "",
        "phone": phone,
        "email": profile.email,
        "website": profile.website,
        "address": address,
        "gstin": profile.gstin,
        "logo_url": logo.cdn_url if logo is not None and logo.cdn_url else None,
        "bank": (
            {"bank": bank.bank, "account_name": bank.account_name, "account_number": bank.account_number, "ifsc": bank.ifsc, "branch": bank.branch, "upi_id": bank.upi_id} if bank is not None else None
        ),
    }


def _lines(agreement: Agreement) -> list[dict]:
    if agreement.pk is None:
        return []
    return [
        {"description": line.description, "quantity": text(line.quantity), "unit": line.unit, "unit_price": text(line.unit_price), "amount": text(line.amount)}
        for line in agreement.lines.all().order_by("sort_order", "id")
    ]


def build(agreement: Agreement, *, company: dict | None = None) -> dict:
    """The document of ``agreement`` as it stands (frozen at issue; a DRAFT preview renders it unfrozen)."""
    customer = agreement.customer
    quotation = None
    if agreement.quotation_version_id:
        version = agreement.quotation_version
        quotation = {"number": version.quotation.number, "version": version.number}
    quotation_number = quotation["number"] if quotation else agreement.legacy_quotation_ref
    return {
        "schema": SCHEMA,
        "agreement": {
            "uid": str(agreement.uid),
            "number": agreement.number,
            "kind": agreement.kind,
            "revision": agreement.revision,
            "language": agreement.language,
            "issued_at": agreement.issued_at.isoformat() if agreement.issued_at else None,
            "supersedes_number": agreement.supersedes.number if agreement.supersedes_id else None,
            "legacy": agreement.legacy,
        },
        "quotation": {"number": quotation_number or "", "version": quotation["version"] if quotation else None},
        "customer": {"uid": str(customer.uid), "code": customer.code, "name": customer.name, "phone": _phone(customer.phone_e164), "address": customer.address},
        "system": {
            "system_type": agreement.system_type,
            "hybrid": is_hybrid(agreement),
            "capacity_kw": text(agreement.capacity_kw),
            "size_label": agreement.size_label,
            "phase": agreement.phase,
            "phase_label": PHASE_LABELS.get(agreement.phase, ""),
            "plant_description": plant_description(agreement),
            "variant": agreement.variant,
            "variant_label": agreement.get_variant_display() if agreement.variant else "",
        },
        "equipment": {
            "panel": agreement.panel_label,
            "panel_capacity": agreement.panel_capacity_label,
            "panel_capacity_w": agreement.panel_capacity_w,
            "panel_dcr": agreement.panel_dcr,
            "panel_qty": agreement.panel_qty,
            "inverter_brand": agreement.inverter_brand,
            "inverter_type": agreement.inverter_type,
            "inverter_qty": agreement.inverter_qty,
            "battery": agreement.battery_label,
            "battery_qty": agreement.battery_qty,
            "structure_material": agreement.structure_material,
            "structure_type": agreement.structure_type,
            "extra_structure": YES_NO[agreement.extra_structure],
            "walkway": YES_NO[agreement.walkway_required],
            "ladder": YES_NO[agreement.ladder_required],
        },
        "prices": {
            "original_price": text(agreement.original_price),
            # the plant price after the offer/discount, without extras: what a Sale Order / Extra Structure prints
            "net_price": text(agreement.original_price - agreement.discount) if agreement.original_price is not None else None,
            "extra_cost": text(agreement.extra_cost) if agreement.extra_cost else None,
            "discount": text(agreement.discount) if agreement.discount else None,
            "final_price": text(agreement.final_price),
            "statutory_fee_label": agreement.statutory_fee_label,
            "statutory_fee_amount": text(agreement.statutory_fee_amount),
            "add_on_offer": agreement.add_on_offer,
            "extra_description": agreement.extra_description,
            "lines": _lines(agreement) if agreement.kind == AgreementKind.EXTRA_STRUCTURE else [],
        },
        "company": company if company is not None else company_block(),
    }


def _phone(e164: str) -> str:
    return e164[3:] if e164.startswith("+91") else e164


def event_payload(agreement: Agreement) -> dict:
    """``agreements.issued`` / ``agreements.superseded`` (contract pinned by ``tests/test_event_contract.py``)."""
    customer = agreement.customer
    version = agreement.quotation_version if agreement.quotation_version_id else None
    return {
        "agreement_uid": str(agreement.uid),
        "number": agreement.number,
        "kind": agreement.kind,
        "version": agreement.revision,
        "customer_uid": str(customer.uid),
        "lead_uid": str(customer.lead.uid) if customer.lead_id else None,
        "quotation_uid": str(version.quotation.uid) if version is not None else None,
        "system_type": agreement.system_type,
        "size_kw": text(agreement.capacity_kw),
        "phase": agreement.phase or None,
        "tier": agreement.variant or None,
        "issued_at": agreement.issued_at.isoformat() if agreement.issued_at else None,
        "supersedes_uid": str(agreement.supersedes.uid) if agreement.supersedes_id else None,
        "fields": {
            "consumer_number": agreement.consumer_number or None,
            "registered_phone": agreement.registered_phone_e164 or None,
            "wheeling_required": agreement.wheeling_required,
            "address": customer.address or None,
            "pincode": customer.pincode or None,
            "district": customer.district or None,
            "location": customer.location or None,
            "panel_uid": str(agreement.panel.uid) if agreement.panel_id else None,
            "panel_capacity_w": agreement.panel_capacity_w,
            "inverter_uid": str(agreement.inverter.uid) if agreement.inverter_id else None,
            "battery_uid": str(agreement.battery.uid) if agreement.battery_id else None,
            "structure_type": agreement.structure_type or None,
            "structure_material": agreement.structure_material or None,
            "quotation_version_uid": str(version.uid) if version is not None else None,
        },
    }
