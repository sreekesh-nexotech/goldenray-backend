"""What a quotation version asks for, validated against the current PackRelease, and the context documents the
pipeline needs (customer, company master, branding, validity policy) in the shapes the Flarize engines read."""

from __future__ import annotations

import re
from decimal import Decimal

from accounts.services.authz import can
from core.errors import Conflict, DomainError, PermissionDenied
from quotations.models import ContentStatus, ContentVersion, RoofType, SubsidyType, SystemType, Tier
from quotations.services.common import ENGINE_SYSTEM, ENGINE_TIER, MODULE, TIER_ORDER, iso, phase_for, size_kw_of

APPLIANCE_ID_RE = re.compile(r"^[a-z0-9_]{2,32}$")
MAX_APPLIANCE_ROWS = 12
VERSION_FIELDS = (
    "system_type",
    "tier",
    "size_key",
    "phase",
    "battery_config",
    "future_size_key",
    "roof_type",
    "distance_km",
    "vehicle_type",
    "subsidy_type",
    "ghs_houses",
    "language",
    "selections",
)


def current_pack_release():
    from packs.services.releases import current_release

    release = current_release()
    if release is None:
        raise Conflict("no_pack_release", "No PackRelease is published; quotations cannot be priced.")
    return release


def current_content_release() -> ContentVersion | None:
    return ContentVersion.objects.filter(status=ContentStatus.PUBLISHED).first()


def _error(field: str, message: str) -> DomainError:
    return DomainError("validation_error", message, errors={field: [message]})


def _template(release, system_type: str) -> dict:
    return ((release.config_version.config or {}).get("bomTemplates") or {}).get(ENGINE_SYSTEM[system_type]) or {}


def _appliance_rows(rows) -> list | None:
    if rows in (None, []):
        return None
    if not isinstance(rows, list) or len(rows) > MAX_APPLIANCE_ROWS:
        raise _error("selections.appliance_rows", f"A list of at most {MAX_APPLIANCE_ROWS} rows.")
    clean = []
    for index, row in enumerate(rows):
        if not isinstance(row, dict) or not APPLIANCE_ID_RE.match(str(row.get("id") or "")):
            raise _error(f"selections.appliance_rows[{index}].id", "An appliance id (2–32 lower-case letters, digits, _).")
        try:
            qty, hours = Decimal(str(row.get("qty"))), Decimal(str(row.get("hours")))
        except Exception:  # noqa: BLE001 - any unparsable value is a validation error
            raise _error(f"selections.appliance_rows[{index}]", "qty and hours must be numbers.") from None
        if not (0 <= qty <= 50) or qty != qty.to_integral_value():
            raise _error(f"selections.appliance_rows[{index}].qty", "A whole number 0–50.")
        if not (0 <= hours <= 24) or (hours * 4) != (hours * 4).to_integral_value():
            raise _error(f"selections.appliance_rows[{index}].hours", "0–24 hours in steps of 0.25.")
        clean.append({"id": row["id"], "qty": int(qty), "hours": int(hours) if hours == hours.to_integral_value() else float(hours)})
    return clean


SELECTION_KEYS = ("tier_selections", "offer_code", "appliance_rows", "validity_override_days")


def _selections(raw, *, user, previous: dict | None = None) -> dict:
    raw = raw or {}
    if not isinstance(raw, dict):
        raise _error("selections", "An object.")
    unknown = set(raw) - set(SELECTION_KEYS)
    if unknown:
        raise _error("selections", f"Unknown keys: {', '.join(sorted(unknown))}.")
    out: dict = {}
    tier_selections = raw.get("tier_selections") or {}
    if not isinstance(tier_selections, dict):
        raise _error("selections.tier_selections", "An object {tier: {category: component id}}.")
    for tier, chosen in tier_selections.items():
        if tier not in TIER_ORDER or not isinstance(chosen, dict) or not all(isinstance(key, str) and isinstance(value, str) for key, value in chosen.items()):
            raise _error("selections.tier_selections", "Use {base|value|premium: {category: component id}}.")
    if tier_selections:
        out["tier_selections"] = tier_selections
    if raw.get("offer_code"):
        out["offer_code"] = str(raw["offer_code"])[:50]
    rows = _appliance_rows(raw.get("appliance_rows"))
    if rows:
        out["appliance_rows"] = rows
    override = raw.get("validity_override_days")
    if override not in (None, ""):
        # an override already on the draft (set by an approver) is kept by any editor; setting or changing one needs approve
        if override != (previous or {}).get("validity_override_days") and not can(user, MODULE, "approve"):
            raise PermissionDenied("validity_override_denied", "Overriding the validity needs quotations.approve.")
        if not isinstance(override, int) or isinstance(override, bool) or not (1 <= override <= 365):
            raise _error("selections.validity_override_days", "Whole days, 1–365.")
        out["validity_override_days"] = override
    return out


def resolve_fields(data: dict, *, release, user, base: dict | None = None) -> dict:
    """The version columns for ``data`` (merged over ``base``), validated against ``release``'s packs."""
    values = {**(base or {}), **{name: data[name] for name in VERSION_FIELDS if name in data}}
    system_type = values.get("system_type")
    if system_type not in SystemType.values:
        raise _error("system_type", "ONGRID or HYBRID.")
    if values.get("tier") not in Tier.values:
        raise _error("tier", "BASE, VALUE or PREMIUM.")
    template = _template(release, system_type)
    size_key = str(values.get("size_key") or "")
    if size_key not in (template.get("sizes") or {}):
        raise _error("size_key", f"Not a size of the {system_type} template: {', '.join(template.get('sizes') or {}) or 'none'}.")
    phase = values.get("phase") or phase_for(size_key, template.get("threePhase") or [])
    if phase not in ("1P", "3P"):
        raise _error("phase", "1P or 3P.")
    battery = str(values.get("battery_config") if values.get("battery_config") is not None else "")
    if system_type == SystemType.HYBRID and battery not in ("0", "1", "2"):
        raise _error("battery_config", "A hybrid system needs 0, 1 or 2 batteries.")
    if system_type == SystemType.ONGRID and battery:
        raise _error("battery_config", "An on-grid system has no battery configuration.")
    future = str(values.get("future_size_key") or "")
    if future and future not in (template.get("sizes") or {}):
        raise _error("future_size_key", "Not a size of the template.")
    roof = values.get("roof_type") or RoofType.FLAT
    if roof not in RoofType.values:
        raise _error("roof_type", "FLAT, SHEET or ELEVATED.")
    distance = Decimal(str(values.get("distance_km") if values.get("distance_km") is not None else "0"))
    if distance < 0 or distance > Decimal("5000"):
        raise _error("distance_km", "0–5000 km (one way, warehouse → site).")
    vehicles = ((release.config_version.config or {}).get("transportConfig") or {}).get("vehicles") or []
    vehicle = str(values.get("vehicle_type") or "")
    if not vehicle and len(vehicles) == 1:
        vehicle = vehicles[0].get("vehicleType") or ""
    if not vehicle:
        raise _error("vehicle_type", "Choose the transport vehicle.")
    if vehicle not in [item.get("vehicleType") for item in vehicles]:
        raise _error("vehicle_type", f"Not a configured vehicle: {', '.join(str(item.get('vehicleType')) for item in vehicles) or 'none'}.")
    subsidy = values.get("subsidy_type") or SubsidyType.NONE
    if subsidy not in SubsidyType.values:
        raise _error("subsidy_type", "residential, ghs or none.")
    houses = values.get("ghs_houses")
    if subsidy == SubsidyType.GHS and not (isinstance(houses, int) and houses >= 1):
        raise _error("ghs_houses", "A group-housing subsidy needs the number of houses.")
    language = values.get("language") or "en"
    if language not in ("en", "ml"):
        raise _error("language", "en or ml.")
    from packs.models import ReleasePack

    exists = ReleasePack.objects.filter(release=release, system_type=system_type, tier=values["tier"], size_key=size_key, phase=phase, battery_config=battery, future_size_key=future).exists()
    if not exists:
        raise Conflict(
            "no_approved_package",
            f"PackRelease #{release.number} has no {system_type} {size_key} {values['tier']} {phase} pack"
            f"{f' (future-ready {future})' if future else ''}{f' with {battery} batteries' if battery else ''}.",
        )
    return {
        "system_type": system_type,
        "tier": values["tier"],
        "size_key": size_key,
        "size_kw": size_kw_of(size_key),
        "phase": phase,
        "battery_config": battery,
        "future_size_key": future,
        "structure_type": {"FLAT": "flatRoof", "SHEET": "sheetRoof", "ELEVATED": "elevated"}[roof],
        "roof_type": roof,
        "distance_km": distance,
        "vehicle_type": vehicle,
        "subsidy_type": subsidy,
        "ghs_houses": houses if subsidy == SubsidyType.GHS else None,
        "language": language,
        "selections": _selections(values.get("selections"), user=user, previous=(base or {}).get("selections")),
    }


def version_inputs(version) -> dict:
    """The inputs of ``version`` as a base for an edit or a revision (bookkeeping keys of imported selections, such as
    ``legacy_quotation_id``, and empty values are not inputs)."""
    values = {name: getattr(version, name) for name in VERSION_FIELDS}
    values["selections"] = {key: value for key, value in (version.selections or {}).items() if key in SELECTION_KEYS and value not in (None, "", [], {})}
    return values


# ── context documents in the Flarize shapes ────────────────────────────────────────────────────────────────────────


def customer_document(customer) -> dict:
    """The quotation record's ``customer`` (``draftCustomer``): identity, contact, the bill the energy engine reads."""
    phone = customer.phone_e164 or ""
    local = phone[3:] if phone.startswith("+91") else phone
    cycle = {"MONTHLY": "monthly", "BIMONTHLY": "bimonthly"}.get(customer.bill_cycle, "monthly")
    bill = customer.current_bill
    return {
        "customerId": customer.code,
        "customerName": customer.name,
        "name": customer.name,
        "phone": local or None,
        "address": customer.address or None,
        "pincode": customer.pincode or None,
        "email": customer.email or None,
        "district": customer.district or None,
        "currentBillAmount": (int(bill) if bill == bill.to_integral_value() else float(bill)) if bill is not None else None,
        "currentBillCycle": cycle,
    }


def company_document() -> dict | None:
    """``quotationShape`` of the company master (D-9: bank and UPI from the primary ``company_bank_account``)."""
    from company.services.bank_accounts import primary_account
    from company.services.profile import get_profile

    profile = get_profile()
    if profile is None:
        return None
    bank = primary_account()

    def asset(field):
        value = getattr(profile, field, None)
        return value.cdn_url if value is not None and getattr(value, "cdn_url", "") else None

    phone = profile.phone_e164[3:] if profile.phone_e164.startswith("+91") else profile.phone_e164
    return {
        "available": True,
        "source": "COMPANY_MASTER",
        "value": {
            "companyName": profile.display_name or None,
            "legalName": profile.legal_name or None,
            "brandName": profile.trade_name or None,
            "parentBrandLine": None,
            "gstNumber": profile.gstin or None,
            "companyRegistration": profile.cin or None,
            "address": profile.address_line or None,
            "city": profile.address_locality or None,
            "state": profile.address_region or None,
            "pincode": profile.postal_code or None,
            "phone": phone or None,
            "email": profile.email or None,
            "website": profile.website or None,
            "bankName": bank.bank if bank else None,
            "bankAccountName": bank.account_name if bank else None,
            "bankAccountNumber": bank.account_number if bank else None,
            "bankIfsc": bank.ifsc if bank else None,
            "bankBranch": (bank.branch or None) if bank else None,
            "upiId": (bank.upi_id or None) if bank else None,
            "upiQrAssetId": str(profile.upi_qr.uid) if profile.upi_qr_id else None,
            "upiQrImageUri": asset("upi_qr"),
            "paymentInstructions": None,
            "signatureAssetId": str(profile.signature.uid) if profile.signature_id else None,
            "signatureImageUri": None,
            "sealAssetId": str(profile.seal.uid) if profile.seal_id else None,
            "sealImageUri": asset("seal"),
            "logoImageUri": asset("logo"),
            "completedInstallations": None,
            "yearsExperience": None,
            "mnreEmpanelled": None,
        },
    }


def branding_store() -> dict | None:
    """The v2 branding store the freeze reads, built from the company master (D-9): the primary bank account (and its
    UPI id) as the only account, each version keyed by the row's version — never a demo account."""
    from company.services.bank_accounts import primary_account

    bank = primary_account()
    if bank is None:
        return None
    version_id = f"BK-{bank.uid}-v{bank.version}"
    store = {
        "schema": "flarize.quotation-branding/2",
        "bank": {
            "primary": str(bank.uid),
            "accounts": {
                str(bank.uid): {
                    "accountId": str(bank.uid),
                    "label": bank.label,
                    "status": "ACTIVE",
                    "isDemo": False,
                    "current": version_id,
                    "versions": {
                        version_id: {
                            "versionId": version_id,
                            "publishedAt": iso(bank.updated_at),
                            "publishedBy": str(bank.updated_by.uid) if bank.updated_by_id else None,
                            "isDemo": False,
                            "values": {"bankName": bank.bank, "accountName": bank.account_name, "accountNumber": bank.account_number, "ifsc": bank.ifsc, "branch": bank.branch or None},
                        }
                    },
                }
            },
        },
    }
    if bank.upi_id:
        upi_version = f"UPI-{bank.uid}-v{bank.version}"
        store["upi"] = {
            "primary": str(bank.uid),
            "accounts": {
                str(bank.uid): {
                    "accountId": str(bank.uid),
                    "label": bank.label,
                    "status": "ACTIVE",
                    "isDemo": False,
                    "current": upi_version,
                    "versions": {upi_version: {"versionId": upi_version, "publishedAt": iso(bank.updated_at), "publishedBy": None, "isDemo": False, "values": {"upiId": bank.upi_id}}},
                }
            },
        }
    return store


def policy_store() -> dict | None:
    """``quotation-policy.json`` (schema 2) from ``pricing_validity_policy``: the DEFAULT days and the ACTIVE windows."""
    from pricing.services.validity import policy_payload

    policy = policy_payload()
    if policy is None:
        return None
    return {
        "schema": "flarize.quotation-policy/2",
        "validity": {"defaultDays": policy["days"], "version": f"DEFAULT@{policy['effective_from']}", "status": "CONFIGURED"},
        "policies": [
            {
                "policyId": window["policy_id"],
                "validityDays": window["days"],
                "effectiveFrom": window["effective_from"],
                "effectiveTo": window["effective_to"],
                "status": window["status"],
                "version": f"{window['policy_id']}@{window['effective_from']}",
            }
            for window in policy.get("windows") or []
        ],
    }


def display_name(user) -> str:
    return (user.get_full_name() or user.email or "").strip() if user is not None else ""


def engine_tier(tier: str) -> str:
    return ENGINE_TIER[tier]
