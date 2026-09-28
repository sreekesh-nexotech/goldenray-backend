"""Import of the legacy main backend's lead tables (PLAN §7.3) — called by ``migrations_tools`` (``import_backend``).

Contract: :mod:`customers.services.import_support` (plain row dicts with the legacy column names in, ``{"created",
"updated", "skipped", "violations"}`` out, idempotent through ``core_legacy_map`` as ``BACKEND`` / ``<table>`` /
``<id>``, timestamps preserved, ``dry_run``, one audit row per call). Order (:func:`import_all`): affiliate
applications and warranty requests first, then installations, then ``lead_collection_home`` (whose referral and
warranty rows are mirrors of the first two).

``lead_collection_home`` → ``leads_lead``
    ``source`` → ``form`` (upper-cased) and ``kind`` (footer/home_booking → HOME_ENQUIRY, contact_page/other →
    CONTACT, group_purchase → GROUP_PURCHASE, quotation → QUOTE_REQUEST, quote_request → ADVANCED_CALC,
    referral_partner → REFERRAL, warranty_service → CONTACT); ``page`` → ``source_url``; ``details`` → ``payload.details``;
    ``phone_number`` → ``phone_e164``; ``quote_request`` rows were created by a Twilio-approved ``verify-otp`` so
    ``otp_verified_at = created_at`` (the "OTP verified flag"); status NEW, or LOST with a reason when older than 90
    days (PLAN §7.3); a new ``L-<n>`` number in legacy id order; linked to the live customer with the same phone.
    A ``referral_partner`` / ``warranty_service`` row that mirrors an imported affiliate application / warranty request
    (same phone, created within 5 minutes — the legacy ``record_lead`` copy) is skipped (``mirrored_submission``):
    its data lives in that row. A mirror without its source row is imported as a lead.
``affiliate_application`` → ``leads_affiliate_application``; ``warranty_service_request`` → ``leads_warranty_request``
    labels → codes (``Real Estate Agent`` → REAL_ESTATE_AGENT …); status NEW; the warranty request is linked to the
    customer with its phone. A row without a usable phone or district cannot be stored (skipped, listed).
``customer_installations`` → ``leads_customer_installation``
    ``system_size`` → ``capacity_kw``, ``installation_date`` → ``installed_on``, ``status`` upper-cased; ``district``
    from the pincode directory; ``is_showcase`` false (no legacy column says otherwise — staff choose showcase rows).
``solar_installations``, ``solar_installation_new``
    Not installations: the legacy calculators' sizing/price tables (no customer, place or date). Every row is reported
    (``not_an_installation``) and nothing is written; their columns' successors are listed in
    ``docs/decisions/leads-customers.md`` (PLAN §7.3 correction, see DEVIATIONS).
"""

from __future__ import annotations

import datetime as dt
from datetime import timedelta
from decimal import Decimal, InvalidOperation

from django.db import transaction
from django.utils import timezone

from core.models import LegacyMap
from customers.services.customers import find_by_phone
from customers.services.import_support import ImportRun, date_value, run_import, timestamp, upsert
from customers.services.phones import try_normalise
from leads.models import AffiliateApplication, CustomerInstallation, IssueType, KeralaDistrict, Lead, LeadEvent, Profession, WarrantyRequest
from leads.models.choices import by_label
from leads.services import pincode_directory
from leads.services.installations import CACHE_NAMESPACE as INSTALLATIONS_NAMESPACE
from leads.services.intake import FORM_KIND, PayloadError, clean_details
from leads.services.leads import CACHE_NAMESPACE

BACKEND = LegacyMap.SourceSystem.BACKEND
ACTION = "leads.legacy_import"
STALE_AFTER = timedelta(days=90)
STALE_REASON = "Imported from the legacy Enquiries inbox without follow-up for more than 90 days."
MIRROR_WINDOW = timedelta(minutes=5)
PROFESSIONS = by_label(Profession)
ISSUES = by_label(IssueType)
DISTRICTS = by_label(KeralaDistrict)
INSTALLATION_STATUS = {"completed": CustomerInstallation.Status.COMPLETED, "in_progress": CustomerInstallation.Status.IN_PROGRESS, "planned": CustomerInstallation.Status.PLANNED}


def _by_id(row: dict) -> int:
    return int(row["id"])


def _text(value, limit: int) -> str:
    return str(value).strip()[:limit] if value not in (None, "") else ""


def _phone(run: ImportRun, row: dict, column: str) -> tuple[str, str]:
    """``(e164 or "", raw)``; an unparsable number is reported."""
    raw = _text(row.get(column), 32)
    phone = try_normalise(raw) if raw else None
    if raw and phone is None:
        run.violation(row["id"], "unparsable_phone", f"{column}={raw!r} is not a valid phone number.")
    return phone or "", raw


# ── affiliate_application ───────────────────────────────────────────────────────────────────────────────────────────
def _import_affiliate(run: ImportRun, row: dict) -> None:
    phone, _raw = _phone(run, row, "phone")
    district = DISTRICTS.get(_text(row.get("district"), 64).casefold())
    if not phone or district is None:
        run.violation(row["id"], "incomplete_row", "a valid phone and a Kerala district are required; row skipped.")
        return
    profession = PROFESSIONS.get(_text(row.get("profession"), 64).casefold())
    if profession is None:
        run.violation(row["id"], "unknown_profession", f"profession={row.get('profession')!r} imported as OTHER.")
        profession = Profession.OTHER
    values = {"full_name": _text(row.get("full_name"), 255) or "—", "phone_e164": phone, "email": _text(row.get("email"), 254).lower(), "profession": profession, "district": district}
    created_at = timestamp(row.get("created_at"))
    upsert(run, AffiliateApplication, row["id"], target=run.find_target(AffiliateApplication, row["id"]), values=values, created_at=created_at, updated_at=created_at)


# ── warranty_service_request ────────────────────────────────────────────────────────────────────────────────────────
def _import_warranty(run: ImportRun, row: dict) -> None:
    phone, _raw = _phone(run, row, "phone")
    if not phone:
        run.violation(row["id"], "incomplete_row", "a valid phone number is required; row skipped.")
        return
    issue = ISSUES.get(_text(row.get("issue_type"), 64).casefold())
    if issue is None:
        run.violation(row["id"], "unknown_issue_type", f"issue_type={row.get('issue_type')!r} imported as OTHER.")
        issue = IssueType.OTHER
    target = run.find_target(WarrantyRequest, row["id"])
    customer = find_by_phone(phone)
    values = {"full_name": _text(row.get("full_name"), 255) or "—", "phone_e164": phone, "issue_type": issue, "description": _text(row.get("description"), 100_000)}
    if target is None or target.customer_id is None:
        values["customer_id"] = customer.pk if customer else None
    created_at = timestamp(row.get("created_at"))
    upsert(run, WarrantyRequest, row["id"], target=target, values=values, created_at=created_at, updated_at=created_at)


# ── customer_installations ──────────────────────────────────────────────────────────────────────────────────────────
def _import_installation(run: ImportRun, row: dict) -> None:
    pincode = _text(row.get("pincode"), 6)
    status = INSTALLATION_STATUS.get(_text(row.get("status"), 50).lower())
    try:
        capacity = Decimal(str(row.get("system_size"))).quantize(Decimal("0.001"))
        installed_on = date_value(row.get("installation_date"))
    except (InvalidOperation, ValueError):
        capacity, installed_on = None, None
    if not pincode or status is None or capacity is None or capacity <= 0 or installed_on is None:
        run.violation(row["id"], "incomplete_row", "pincode, a known status, a positive system_size and installation_date are required; row skipped.")
        return
    phone, raw = _phone(run, row, "phone_number")
    target = run.find_target(CustomerInstallation, row["id"])
    address = _text(row.get("address"), 100_000)
    if raw and not phone:  # the legacy form never validated phones: keep the number as recorded beside the address
        address = "\n".join(part for part in (address, f"(phone as recorded: {raw})") if part)
    values = {
        "customer_name": _text(row.get("customer_name"), 255) or "—",
        "phone_e164": phone,
        "pincode": pincode,
        "address": address,
        "capacity_kw": capacity,
        "installed_on": installed_on,
        "status": status,
    }
    if target is None or not target.district:
        values["district"] = pincode_directory.current().district_for(pincode) or ""
    upsert(run, CustomerInstallation, row["id"], target=target, values=values, created_at=timestamp(row.get("created_at")), updated_at=timestamp(row.get("updated_at")))


# ── lead_collection_home ────────────────────────────────────────────────────────────────────────────────────────────
def _is_mirror(form: str, phone: str, created_at) -> bool:
    model = {Lead.Form.REFERRAL_PARTNER: AffiliateApplication, Lead.Form.WARRANTY_SERVICE: WarrantyRequest}.get(form)
    if model is None or not phone or created_at is None:
        return False
    return model.all_objects.filter(phone_e164=phone, created_at__range=(created_at - MIRROR_WINDOW, created_at + MIRROR_WINDOW)).exists()


def _import_lead(run: ImportRun, row: dict, *, today: dt.datetime) -> None:
    source = _text(row.get("source"), 32).upper() or Lead.Form.OTHER
    if source not in FORM_KIND:
        run.violation(row["id"], "unknown_source", f"source={row.get('source')!r} imported as OTHER.")
        source = Lead.Form.OTHER
    phone, raw = _phone(run, row, "phone_number")
    created_at = timestamp(row.get("created_at")) or today
    target = run.find_target(Lead, row["id"])
    if target is None and _is_mirror(source, phone, created_at):
        run.skipped += 1
        run.violation(row["id"], "mirrored_submission", f"copy of an imported {source.lower()} submission (legacy record_lead); not imported as a lead.")
        return
    try:
        details = clean_details(row.get("details"))
    except PayloadError:
        run.violation(row["id"], "invalid_details", "details is not a flat object of scalars; kept as text.")
        details = {"Details (as recorded)": str(row.get("details"))[:2000]}
    if raw and not phone:
        details["Phone (as recorded)"] = raw
    name = _text(row.get("name"), 255)
    if not name:
        run.violation(row["id"], "blank_name", "name is empty; imported as '—'.")
    values = {
        "kind": FORM_KIND[source],
        "form": source,
        "name": name or "—",
        "phone_e164": phone,
        "source_url": _text(row.get("page"), 500),
        "payload": {"details": details} if details else {},
        "otp_verified_at": created_at if source == Lead.Form.QUOTE_REQUEST else None,
    }
    if target is None:
        stale = today - created_at > STALE_AFTER
        customer = find_by_phone(phone)
        values.update(
            number=_next_lead_number(),
            status=Lead.Status.LOST if stale else Lead.Status.NEW,
            lost_reason=STALE_REASON if stale else "",
            customer_id=customer.pk if customer else None,
            assignee_id=customer.owner_id if customer else None,
        )
    lead = upsert(run, Lead, row["id"], target=target, values=values, created_at=created_at, updated_at=timestamp(row.get("updated_at")) or created_at)
    if target is None:
        LeadEvent.objects.create(lead=lead, event=LeadEvent.Event.IMPORTED, at=created_at, data={"source_table": "lead_collection_home", "source_id": str(row["id"]), "source": row.get("source")})


def _next_lead_number() -> str:
    from core.sequences import next_number

    return next_number("LEAD")


# ── solar_installations / solar_installation_new ────────────────────────────────────────────────────────────────────
def _report_sizing_row(run: ImportRun, row: dict) -> None:
    run.skipped += 1
    run.violation(
        row["id"],
        "not_an_installation",
        f"calculator sizing row (power_capacity={row.get('power_capacity')} kW, total_cost={row.get('total_cost')}); nothing written — see docs/decisions/leads-customers.md.",
    )


# ── Entry points ────────────────────────────────────────────────────────────────────────────────────────────────────
def _run(table: str, rows, fn, *, user, dry_run: bool, object_type: str, namespaces=(CACHE_NAMESPACE,)) -> dict:
    return run_import(ImportRun(BACKEND, table), rows, fn, user=user, dry_run=dry_run, action=ACTION, object_type=object_type, namespaces=namespaces, order=_by_id)


def import_affiliate_applications(rows: list[dict], *, user=None, dry_run: bool = False) -> dict:
    return _run("affiliate_application", rows, _import_affiliate, user=user, dry_run=dry_run, object_type="leads.affiliateapplication")


def import_warranty_requests(rows: list[dict], *, user=None, dry_run: bool = False) -> dict:
    return _run("warranty_service_request", rows, _import_warranty, user=user, dry_run=dry_run, object_type="leads.warrantyrequest")


def import_customer_installations(rows: list[dict], *, user=None, dry_run: bool = False) -> dict:
    return _run("customer_installations", rows, _import_installation, user=user, dry_run=dry_run, object_type="leads.customerinstallation", namespaces=(INSTALLATIONS_NAMESPACE,))


def import_leads(rows: list[dict], *, user=None, dry_run: bool = False, today: dt.datetime | None = None) -> dict:
    now = today or timezone.now()
    return _run("lead_collection_home", rows, lambda run, row: _import_lead(run, row, today=now), user=user, dry_run=dry_run, object_type="leads.lead")


def report_solar_installations(rows: list[dict], *, table: str = "solar_installations", user=None, dry_run: bool = False) -> dict:
    """``solar_installations`` / ``solar_installation_new``: every row reported (and audited), nothing else written."""
    return _run(table, rows, _report_sizing_row, user=user, dry_run=dry_run, object_type="leads.customerinstallation", namespaces=())


def import_all(tables: dict[str, list[dict]], *, user=None, dry_run: bool = False, today: dt.datetime | None = None) -> dict[str, dict]:
    """Every leads table in dependency order, in one transaction (rolled back when ``dry_run``)."""
    with transaction.atomic():
        results = {
            "affiliate_application": import_affiliate_applications(tables.get("affiliate_application", []), user=user),
            "warranty_service_request": import_warranty_requests(tables.get("warranty_service_request", []), user=user),
            "customer_installations": import_customer_installations(tables.get("customer_installations", []), user=user),
            "lead_collection_home": import_leads(tables.get("lead_collection_home", []), user=user, today=today),
            "solar_installations": report_solar_installations(tables.get("solar_installations", []), user=user),
            "solar_installation_new": report_solar_installations(tables.get("solar_installation_new", []), table="solar_installation_new", user=user),
        }
        if dry_run:
            transaction.set_rollback(True)
    return results
