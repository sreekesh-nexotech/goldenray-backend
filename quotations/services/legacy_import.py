"""Legacy import into the quotations tables (PLAN §7.3, §7.4) — called by ``migrations_tools`` (``import_flarize``,
``import_backend``).

Contract (:mod:`customers.services.import_support`): every function takes plain rows (the parsed legacy JSON or
database rows as dicts) and returns ``{"created", "updated", "skipped", "violations"}``; idempotent through
``core_legacy_map`` (re-running updates, never duplicates); violations are listed, never raised; ``dry_run`` rolls back.

Order: :func:`import_flarize_inclusions`, :func:`import_flarize_tier_names`, :func:`import_flarize_testimonials` /
:func:`import_backend_testimonials`, then :func:`import_flarize_content` (publishing freezes the masters beside the
content), :func:`import_flarize_branding`, :func:`import_flarize_counter` and :func:`import_flarize_quotations`
(customers and users first), :func:`import_sent_quotes`.

Frozen documents are imported **byte for byte**: ``quotations_version.document_payload`` is the legacy document as
parsed from ``quotation-state.json`` (nothing re-derived), ``document_payload_sha256`` its canonical SHA-256, the version
``legacy = true`` with no PackRelease (the documents predate releases). Re-rendering replays that payload only.
"""

from __future__ import annotations

import re
from decimal import Decimal, InvalidOperation

from django.db import transaction
from django.utils import timezone

from audit.services import record
from core.models import LegacyMap
from core.sequences import ensure_next_value_at_least
from customers.services.import_support import ImportRun, checksum, date_value, mapped_id, run_import, timestamp, upsert
from engines.frozen import sha256_hex
from flarize.cache_utils import bump
from quotations.models import (
    BomSnapshot,
    CommercialSnapshot,
    ContentStatus,
    ContentVersion,
    EmailChannel,
    EmailLog,
    EmailStatus,
    Inclusion,
    InclusionKind,
    Quotation,
    QuotationStatus,
    Testimonial,
    TierDisplayName,
    Version,
    VersionStatus,
)
from quotations.services.common import CACHE_NAMESPACE, CONTENT_NAMESPACE, PLATFORM_SYSTEM, PLATFORM_TIER, PUBLIC_TESTIMONIALS_NAMESPACE, money

FLARIZE = LegacyMap.SourceSystem.FLARIZE
BACKEND = LegacyMap.SourceSystem.BACKEND
ACTION = "quotations.legacy_import"
SIZE_KEY_RE = re.compile(r"^[0-9]+(\.[0-9]{1,2})?(sp|tp)?$")
COMPONENT_LABELS = {
    "solarPanels": "Solar panels",
    "inverter": "Inverter",
    "battery": "Battery",
    "mountingStructure": "Mounting structure",
    "dcdb": "DC distribution box",
    "acdb": "AC distribution box",
    "dcCables": "DC cables",
    "acCables": "AC cables",
    "earthing": "Earthing",
    "lightningProtection": "Lightning protection",
    "installation": "Installation",
    "commissioning": "Commissioning",
    "netMeteringAssistance": "Net-metering assistance",
    "transportation": "Transportation",
    "monitoring": "Monitoring",
    "warranty": "Warranty",
    "amc": "AMC",
}
TIER_FILE_KEYS = {"Base": "BASE", "Value": "VALUE", "Premium": "PREMIUM"}


def _user(source_id) -> int | None:
    return mapped_id(FLARIZE, "users", source_id) if source_id else None


def _service_key(label: str) -> str:
    words = re.sub(r"[^A-Za-z0-9]+", " ", label).split()
    return ("svc_" + "_".join(word.lower() for word in words))[:64] or "svc_row"


# ── inclusions / tier names / testimonials / content ─────────────────────────────────────────────────────────────


def import_flarize_inclusions(matrix: dict, *, user=None, dry_run: bool = False) -> dict:
    """``quotation-inclusions.json`` → one COMPONENT row per inclusion key (per-tier booleans) and one SERVICE row per
    ``_serviceMatrix`` row (per-tier value or text). Legacy map ``FLARIZE quotation-inclusions.json <key>``."""
    run = ImportRun(FLARIZE, "quotation-inclusions.json")
    rows: list[dict] = []
    tiers = {name: matrix.get(name) or {} for name in TIER_FILE_KEYS}
    keys = []
    for config in tiers.values():
        keys += [key for key in config if not key.startswith("_") and key not in keys]
    for index, key in enumerate(keys):
        rows.append(
            {
                "source_id": key,
                "key": key,
                "kind": InclusionKind.COMPONENT,
                "label_en": COMPONENT_LABELS.get(key, key),
                "applies_to": {TIER_FILE_KEYS[name]: config.get(key) for name, config in tiers.items()},
                "sort_order": index,
            }
        )
    for index, row in enumerate(matrix.get("_serviceMatrix") or []):
        label = str(row.get("label") or "").strip()
        if not label:
            run.violation(index, "incomplete_row", "A service-matrix row without a label is skipped.")
            continue
        rows.append(
            {
                "source_id": f"service:{label}",
                "key": _service_key(label),
                "kind": InclusionKind.SERVICE,
                "label_en": label[:160],
                "applies_to": {tier: row.get(tier.lower()) for tier in ("BASE", "VALUE", "PREMIUM")},
                "sort_order": 100 + index,
            }
        )

    def import_row(run: ImportRun, row: dict) -> None:
        values = {name: row[name] for name in ("key", "kind", "label_en", "applies_to", "sort_order")}
        values["default_on"] = True
        target = run.find_target(Inclusion, row["source_id"], lambda: Inclusion.objects.filter(key=row["key"]).first())
        upsert(run, Inclusion, row["source_id"], target=target, values=values, created_by_id=None)

    return run_import(run, rows, import_row, user=user, dry_run=dry_run, action=ACTION, object_type="quotations.inclusion", namespaces=(CONTENT_NAMESPACE,))


def import_flarize_tier_names(names: dict, *, user=None, dry_run: bool = False) -> dict:
    """``tier-display-names.json`` → one row per (system type, tier) for both system types (Flarize has one set);
    ``recommendedTier`` → ``is_recommended`` with ``recommendedBadge`` as its badge."""
    run = ImportRun(FLARIZE, "tier-display-names.json")
    rows = []
    recommended = str(names.get("recommendedTier") or "").lower()
    for system in ("ONGRID", "HYBRID"):
        for tier in ("base", "value", "premium"):
            name = str(names.get(tier) or "").strip()
            if not name:
                run.violation(tier, "missing_name", f"No display name for tier {tier!r}; skipped.")
                continue
            rows.append({"source_id": f"{system}:{tier}", "system_type": system, "tier": PLATFORM_TIER[tier], "name_en": name[:80], "recommended": tier == recommended})

    def import_row(run: ImportRun, row: dict) -> None:
        values = {
            "system_type": row["system_type"],
            "tier": row["tier"],
            "name_en": row["name_en"],
            "is_recommended": row["recommended"],
            "badge_en": str(names.get("recommendedBadge") or "")[:80] if row["recommended"] else "",
        }
        target = run.find_target(TierDisplayName, row["source_id"], lambda: TierDisplayName.objects.filter(system_type=row["system_type"], tier=row["tier"]).first())
        upsert(run, TierDisplayName, row["source_id"], target=target, values=values)

    return run_import(run, rows, import_row, user=user, dry_run=dry_run, action=ACTION, object_type="quotations.tierdisplayname", namespaces=(CONTENT_NAMESPACE,))


def _decimal(value) -> Decimal | None:
    if value in (None, ""):
        return None
    try:
        number = Decimal(str(value))
    except InvalidOperation:
        return None
    return number if number.is_finite() else None


def import_flarize_testimonials(document: dict, *, user=None, dry_run: bool = False) -> dict:
    """``quotation-testimonials.json`` ``entries`` → testimonials (``show_on_website`` false: these were quotation-only);
    the entry index is the legacy id. ``photoUri`` pointing into Flarize's CMS store is not a public URL (dropped,
    ``photo_not_migrated``)."""
    run = ImportRun(FLARIZE, "quotation-testimonials.json")
    rows = [{**entry, "_index": index} for index, entry in enumerate(document.get("entries") or [])]

    def import_row(run: ImportRun, row: dict) -> None:
        name, quote = str(row.get("name") or "").strip(), str(row.get("quote") or "").strip()
        if not name or not quote:
            run.violation(row["_index"], "incomplete_row", "A testimonial needs a name and a quote; skipped.")
            return
        photo = str(row.get("photoUri") or "")
        if photo and not photo.startswith("https://"):
            run.violation(row["_index"], "photo_not_migrated", f"photoUri={photo!r} is a Flarize CMS asset, not a public URL; left empty.")
            photo = ""
        before, after = _decimal(row.get("billBefore")), _decimal(row.get("billAfter"))
        if before is not None and after is not None and after > before:
            run.violation(row["_index"], "bill_after_above_before", "billAfter is above billBefore; billAfter dropped.")
            after = None
        values = {
            "customer_name": name[:80],
            "location": str(row.get("place") or "")[:80],
            "capacity_kw": _decimal(row.get("systemKw")),
            "installed_on_label": str(row.get("installedOn") or "")[:40],
            "quote_en": quote,
            "bill_before": before,
            "bill_after": after,
            "photo_url": photo,
            "sort_order": row["_index"],
            "is_active": True,
            "show_on_website": False,
        }
        target = run.find_target(Testimonial, row["_index"])
        upsert(run, Testimonial, row["_index"], target=target, values=values, updated_at=timestamp(document.get("updatedAt")), updated_by_id=_user(document.get("updatedBy")))

    return run_import(run, rows, import_row, user=user, dry_run=dry_run, action=ACTION, object_type="quotations.testimonial", namespaces=(CONTENT_NAMESPACE, PUBLIC_TESTIMONIALS_NAMESPACE))


def import_backend_testimonials(rows: list[dict], *, user=None, dry_run: bool = False) -> dict:
    """Main backend ``bom_quotationtestimonial`` → testimonials with ``show_on_website = true`` (PLAN §7.3). An uploaded
    ``photo`` file is not copied here (media import); ``photo_url`` is kept (``photo_file_not_migrated``)."""
    run = ImportRun(BACKEND, "bom_quotationtestimonial")

    def import_row(run: ImportRun, row: dict) -> None:
        if row.get("photo"):
            run.violation(row["id"], "photo_file_not_migrated", f"photo={row['photo']!r} is an uploaded file; import it through media and set the photo.")
        before, after = _decimal(row.get("bill_before")), _decimal(row.get("bill_after"))
        capacity = None
        match = re.match(r"^\s*([0-9]+(?:\.[0-9]+)?)\s*kW", str(row.get("system_label") or ""), re.IGNORECASE)
        if match:
            capacity = Decimal(match.group(1))
        values = {
            "customer_name": str(row.get("name") or "")[:80],
            "location": str(row.get("location") or "")[:80],
            "capacity_kw": capacity,
            "system_label": str(row.get("system_label") or "")[:40],
            "installed_on": date_value(row.get("installed_on")),
            "quote_en": str(row.get("quote") or ""),
            "quote_ml": str(row.get("quote_ml") or ""),
            "bill_before": before,
            "bill_after": after if before is None or after is None or after <= before else None,
            "photo_url": str(row.get("photo_url") or ""),
            "is_active": bool(row.get("is_active", True)),
            "sort_order": int(row.get("sort_order") or 0),
            "show_on_website": True,
        }
        if not values["customer_name"] or not values["quote_en"]:
            run.violation(row["id"], "incomplete_row", "A testimonial needs a name and a quote; skipped.")
            return
        target = run.find_target(Testimonial, row["id"])
        upsert(run, Testimonial, row["id"], target=target, values=values, created_at=timestamp(row.get("created_at")), updated_at=timestamp(row.get("updated_at")))

    return run_import(
        run,
        rows,
        import_row,
        user=user,
        dry_run=dry_run,
        action=ACTION,
        object_type="quotations.testimonial",
        namespaces=(CONTENT_NAMESPACE, PUBLIC_TESTIMONIALS_NAMESPACE),
        order=lambda row: row["id"],
    )


def import_flarize_content(store: dict, *, user=None, dry_run: bool = False) -> dict:
    """``quotation-content.json``: ``published`` → the PUBLISHED content version (its number; the masters imported
    before are frozen beside it), a ``draft`` that differs → the DRAFT (next number). Legacy map
    ``FLARIZE quotation-content.json published:<v>`` / ``draft:<v>``."""
    from quotations.services import content as content_services

    run = ImportRun(FLARIZE, "quotation-content.json")
    rows = []
    published = store.get("published") or {}
    if published.get("content"):
        rows.append({"source_id": f"published:{published.get('version')}", "status": ContentStatus.PUBLISHED, **published})
    draft = store.get("draft") or {}
    if draft.get("content") and draft.get("content") != published.get("content"):
        rows.append({"source_id": f"draft:{draft.get('version')}", "status": ContentStatus.DRAFT, **draft})

    def import_row(run: ImportRun, row: dict) -> None:
        number = int(row.get("version") or 1)
        target = run.find_target(ContentVersion, row["source_id"])
        taken = ContentVersion.all_objects.filter(number=number).exclude(pk=getattr(target, "pk", None)).first()
        if taken is not None:
            run.violation(row["source_id"], "number_taken", f"Content version #{number} already exists and is not this import; skipped.")
            return
        if row["status"] == ContentStatus.PUBLISHED and target is None and ContentVersion.objects.filter(status=ContentStatus.PUBLISHED).exists():
            run.violation(row["source_id"], "published_exists", "Another content version is already PUBLISHED on the platform; imported as SUPERSEDED.")
            row = {**row, "status": ContentStatus.SUPERSEDED}
        report = content_services._fit(row["content"])
        if not report["ok"]:
            run.violation(row["source_id"], "content_does_not_fit", f"{len(report['errors'])} fit error(s) (kept as imported).")
        values = {"number": number, "status": row["status"], "language_payload": row["content"], "fit_report": report}
        if row["status"] != ContentStatus.DRAFT:
            payload = content_services.release_payload()
            values.update(release_payload=payload, release_sha256=sha256_hex(payload), published_at=timestamp(row.get("publishedAt")) or timezone.now(), published_by_id=_user(row.get("publishedBy")))
        if target is not None and target.status != ContentStatus.DRAFT:
            values.pop("release_payload", None)
            values.pop("release_sha256", None)
            values.pop("status", None)
        upsert(run, ContentVersion, row["source_id"], target=target, values=values, updated_at=timestamp(row.get("updatedAt") or row.get("publishedAt")))

    return run_import(run, rows, import_row, user=user, dry_run=dry_run, action=ACTION, object_type="quotations.contentversion", namespaces=(CONTENT_NAMESPACE,))


def import_flarize_branding(store: dict, *, user=None, dry_run: bool = False) -> dict:
    """``quotation-branding-state.json`` (D-9: superseded by ``company_bank_account``, owned by the company package).

    Nothing is written here: every account is reported — demo accounts (``isDemo``) are never migrated
    (``demo_branding_not_migrated``), real ones must be entered as company bank accounts (``enter_as_bank_account``,
    the account number is not repeated in the report). Issued documents keep the branding they froze."""
    run = ImportRun(FLARIZE, "quotation-branding-state.json")
    rows = []
    for kind in ("bank", "upi"):
        for account_id, account in ((store.get(kind) or {}).get("accounts") or {}).items():
            rows.append({"kind": kind, "account_id": account_id, "demo": bool(account.get("isDemo")), "label": account.get("label")})
    for kind in ("signature", "seal"):
        if (store.get(kind) or {}).get("versions"):
            rows.append({"kind": kind, "account_id": kind, "demo": any(v.get("isDemo") for v in store[kind]["versions"].values()), "label": kind})

    def import_row(run: ImportRun, row: dict) -> None:
        run.skipped += 1
        source = f"{row['kind']}:{row['account_id']}"
        if row["demo"]:
            run.violation(source, "demo_branding_not_migrated", f"{row['kind']} account {row['label']!r} is a DEMO account; not migrated.")
        else:
            run.violation(source, "enter_as_bank_account", f"{row['kind']} account {row['label']!r}: enter it as a company bank account (D-9).")

    return run_import(run, rows, import_row, user=user, dry_run=dry_run, action=ACTION, object_type="quotations.branding", namespaces=())


@transaction.atomic
def import_flarize_counter(counter: dict, *, user=None, dry_run: bool = False) -> dict:
    """``quotation-counter.json`` ``{lastNumber}`` → ``core.sequences`` QUO continues after it (never moves back)."""
    run = ImportRun(FLARIZE, "quotation-counter.json")
    last = counter.get("lastNumber")
    if not isinstance(last, int) or last < 0:
        run.violation("lastNumber", "invalid_counter", f"lastNumber={last!r} is not a counter; nothing changed.")
        run.skipped += 1
        return run.as_dict()
    sid = transaction.savepoint()
    before = ensure_next_value_at_least("QUO", 1)
    after = ensure_next_value_at_least("QUO", last + 1)
    if after != before:
        run.updated += 1
    else:
        run.skipped += 1
    record(ACTION, object_type="core.sequencecounter", actor=user, after={"source_table": run.source_table, "kind": "QUO", "next_value": after, "last_legacy": last}, note="FLARIZE import")
    if dry_run:
        transaction.savepoint_rollback(sid)
    else:
        transaction.savepoint_commit(sid)
    return run.as_dict()


# ── quotation-state.json ───────────────────────────────────────────────────────────────────────────────────────────


def _customer(run: ImportRun, qid: str, legacy_customer: dict, user):
    from customers.models import Customer
    from customers.services.legacy_import import match_or_create_by_phone

    legacy_customer = legacy_customer or {}
    pk = mapped_id(FLARIZE, "customers", legacy_customer.get("customerId"))
    if pk is not None:
        customer = Customer.all_objects.filter(pk=pk).first()
        if customer is not None:
            while customer.merged_into_id is not None:
                customer = Customer.all_objects.get(pk=customer.merged_into_id)
            return customer
    customer, created = match_or_create_by_phone(
        phone=legacy_customer.get("phone"), name=legacy_customer.get("customerName") or legacy_customer.get("name") or "", user=user, source=Customer.Source.SALES_ENTRY
    )
    if customer is None:
        run.violation(qid, "no_customer", "The quotation's customer has no imported record and no valid phone; quotation skipped.")
    elif created:
        run.violation(qid, "customer_created", f"Customer {customer.code} created from the quotation (phone match found none).")
    return customer


def _valid_until(value):
    """Flarize ``validUntil``: an ISO timestamp (``2026-09-24T05:00:27.386Z``, the end of validity) → its local date."""
    if isinstance(value, str) and "T" in value:
        return timezone.localtime(timestamp(value)).date()
    return date_value(value)


def _size_key(record_: dict, document: dict | None) -> str:
    pack_inputs = (((document or {}).get("snapshot") or {}).get("pack") or {}).get("inputs") or {}
    size = str(pack_inputs.get("size") or "")
    if SIZE_KEY_RE.match(size):
        return size
    kw = (record_.get("system") or {}).get("systemSizeKw")
    return str(int(kw)) if isinstance(kw, (int, float)) and float(kw).is_integer() else str(kw or "0")


def _version_values(record_: dict, document: dict | None, status: str) -> dict:
    system = record_.get("system") or {}
    pack_inputs = (((document or {}).get("snapshot") or {}).get("pack") or {}).get("inputs") or {}
    pricing = ((((document or {}).get("payload") or {}).get("pricing") or {}).get("customer")) or {}
    phase = system.get("phase")
    battery = pack_inputs.get("batteryConfig") if system.get("systemType") == "hybrid" else None
    values = {
        "number": (document or {}).get("version") or 1,
        "status": status,
        "system_type": PLATFORM_SYSTEM.get(system.get("systemType"), "ONGRID"),
        "tier": PLATFORM_TIER.get(system.get("packageTier") or system.get("tier") or "value", "VALUE"),
        "size_key": _size_key(record_, document),
        "size_kw": Decimal(str(system.get("systemSizeKw") or 0)),
        "phase": "3P" if phase in ("3P", "three") else "1P",
        "battery_config": "" if battery is None else str(battery),
        "future_size_key": str(pack_inputs.get("futureSystemSize") or ""),
        "roof_type": pack_inputs.get("roofType") if pack_inputs.get("roofType") in ("FLAT", "SHEET", "ELEVATED") else "FLAT",
        "distance_km": Decimal(str(pack_inputs.get("distanceKm") or 0)),
        "vehicle_type": str(pack_inputs.get("vehicleType") or "")[:32],
        "subsidy_type": record_.get("subsidyType") if record_.get("subsidyType") in ("residential", "ghs", "none") else "none",
        "ghs_houses": record_.get("ghsHouses"),
        "language": "ml" if record_.get("quotationLanguage") == "ml" else "en",
        "selections": {"appliance_rows": record_.get("applianceRows"), "legacy_quotation_id": record_.get("quotationId")},
        "legacy": True,
        "customer_price_incl_gst": money(pricing.get("sellingPriceIncludingGST")),
        "transport_extra": money(pricing.get("extrasIncludingGST")),
        "final_price": money(pricing.get("customerTotalIncludingGST")),
    }
    if document is not None:
        values.update(
            gate_report=(document.get("payload") or {}).get("generationGate"),
            issued_at=timestamp(document.get("issuedAt")),
            issued_by_id=_user(document.get("issuedBy")),
            document_payload=document,
            document_payload_sha256=sha256_hex(document),
        )
    return values


def _snapshot(run: ImportRun, model, version: Version, tier: str, primary: bool, snapshot_id: str, record_: dict | None, qid: str) -> None:
    if not snapshot_id:
        return
    if record_ is None:
        run.violation(qid, "snapshot_missing", f"{model.__name__} {snapshot_id} is referenced but not in the store.")
        return
    if model is BomSnapshot:
        values = {
            "tier": tier,
            "is_primary": primary,
            "lines": record_.get("lines") or [],
            "lock_acknowledgements": record_.get("acknowledgements") or [],
            "engineering_status": record_.get("validationStatus") if record_.get("validationStatus") in ("VALID", "WARNING", "BLOCKED") else "",
            "record": record_,
            "legacy_ref": snapshot_id,
        }
    else:
        pricing = record_.get("pricing") or {}
        values = {
            "tier": tier,
            "is_primary": primary,
            "cost_lines": {name: record_.get(name) for name in ("cost", "pricing", "pack", "offer")},
            "pins": record_.get("versions") or {},
            "margin_check": {"landedCostCheck": (record_.get("cost") or {}).get("landedCostCheck"), "referenceMarginPct": (record_.get("cost") or {}).get("referenceMarginPct")},
            "customer_total_incl_gst": money(pricing.get("customerTotalIncludingGST")),
            "record": record_,
            "legacy_ref": snapshot_id,
        }
    sub = ImportRun(FLARIZE, f"quotation-state.json:{'bomSnapshots' if model is BomSnapshot else 'commercialSnapshots'}")
    target = sub.find_target(model, snapshot_id, lambda: model.objects.filter(quotation_version=version, tier=tier).first())
    upsert(sub, model, snapshot_id, target=target, values={"quotation_version_id": version.pk, **values}, created_at=timestamp(record_.get("lockedAt") or record_.get("issuedAt")))


def _modified_on_platform(run: ImportRun, qid: str, quotation: Quotation) -> bool:
    """True when the platform wrote the quotation after its last import (accepted, cancelled, expired, revised, …) or
    gave it a version the import did not create. The import stamps ``updated_at`` from the source and links the legacy
    map afterwards, so an imported row's ``updated_at`` never passes its map's ``imported_at``; every platform write
    (``versioned_update``) stamps now. A re-run must never take such a quotation back to the Flarize state."""
    imported_at = LegacyMap.objects.filter(source_system=run.source_system, source_table=run.source_table, source_id=str(qid), target_id=quotation.pk).values_list("imported_at", flat=True).first()
    if imported_at is None:
        return quotation.status not in (QuotationStatus.DRAFT, QuotationStatus.ISSUED)
    if quotation.updated_at > imported_at:
        return True
    versions = set(Version.all_objects.filter(quotation=quotation).values_list("pk", flat=True))
    imported = set(LegacyMap.objects.filter(source_system=FLARIZE, source_table="quotation-state.json:documents", target_id__in=versions).values_list("target_id", flat=True))
    return bool(versions - imported)


def import_flarize_quotations(state: dict, *, user=None, dry_run: bool = False) -> dict:
    """``quotation-state.json`` → quotations, versions (frozen documents byte for byte), BOM and commercial snapshots.

    Customers resolve through ``FLARIZE customers`` (then by phone), owners/issuers through ``FLARIZE users``
    (``unmapped_owner``). Snapshots no quotation refers to (orphans of failed orchestrations) are reported
    (``orphan_snapshot``), not imported. Legacy map ``FLARIZE quotation-state.json <quotationId>``."""
    run = ImportRun(FLARIZE, "quotation-state.json")
    documents = state.get("documents") or {}
    version_status = state.get("versionStatus") or {}
    bom_snapshots = state.get("bomSnapshots") or {}
    commercial_snapshots = state.get("commercialSnapshots") or {}
    rows = list((state.get("quotations") or {}).values())
    referenced = set()
    for row in rows:
        referenced.update(filter(None, [row.get("bomSnapshotId"), row.get("commercialSnapshotId")]))
        for option in row.get("alternativeOptions") or []:
            referenced.update(filter(None, [option.get("bomSnapshotId"), option.get("commercialSnapshotId")]))

    def import_row(run: ImportRun, row: dict) -> None:
        qid = row.get("quotationId")
        if not qid or not row.get("system"):
            run.violation(qid or "?", "incomplete_row", "A quotation needs quotationId and system; skipped.")
            return
        customer = _customer(run, qid, row.get("customer") or {}, user)
        if customer is None:
            return
        owner_id = _user(row.get("salespersonId"))
        if row.get("salespersonId") and owner_id is None:
            run.violation(qid, "unmapped_owner", f"salespersonId={row.get('salespersonId')!r} has no imported user; owner left empty.")
        docs = documents.get(qid) or []
        status = QuotationStatus.ISSUED if row.get("status") == "ISSUED" and docs else QuotationStatus.DRAFT
        number = str(row.get("quotationNumber") or "")[:48] if status != QuotationStatus.DRAFT else ""
        values = {
            "number": number,
            "customer_id": customer.pk,
            "owner_id": owner_id,
            "status": status,
            "valid_until": _valid_until(row.get("validUntil")),
            "issued_at": timestamp(row.get("issuedAt")),
            "source": row.get("quotationSource") if row.get("quotationSource") in ("DIRECT", "AFFILIATE", "DISTRICT") else "DIRECT",
            "district": str(row.get("district") or "")[:100],
            "affiliate_ref": str(row.get("affiliateId") or "")[:64] if row.get("quotationSource") == "AFFILIATE" else "",
            "legacy": True,
            "legacy_ref": qid,
        }
        target = run.find_target(Quotation, qid, lambda: Quotation.all_objects.filter(legacy_ref=qid).first())
        if target is not None and _modified_on_platform(run, qid, target):
            run.skipped += 1
            run.violation(qid, "modified_on_platform", f"Quotation {target.number or qid} changed on the platform after it was imported ({target.status}); left as it is.")
            return
        quotation = upsert(
            run, Quotation, qid, target=target, values=values, created_at=timestamp(row.get("createdAt")), updated_at=timestamp(row.get("updatedAt")), created_by_id=_user(row.get("createdBy"))
        )
        current = None
        versions_run = ImportRun(FLARIZE, "quotation-state.json:documents")
        for document in docs or [None]:
            number_v = (document or {}).get("version") or 1
            vstatus = version_status.get(f"{qid}#{number_v}") if document is not None else None
            vstatus = vstatus if vstatus in (VersionStatus.ISSUED, VersionStatus.SUPERSEDED) else (VersionStatus.ISSUED if document is not None else VersionStatus.DRAFT)
            vvalues = {"quotation_id": quotation.pk, **_version_values(row, document, vstatus)}
            source_id = f"{qid}#{number_v}"
            vtarget = versions_run.find_target(Version, source_id, lambda: Version.all_objects.filter(quotation=quotation, number=number_v).first())
            if vtarget is not None and vtarget.document_payload_sha256 and document is not None and vtarget.document_payload_sha256 != vvalues["document_payload_sha256"]:
                run.violation(source_id, "frozen_document_changed", "The stored frozen document differs from the source; the frozen copy is kept.")
                for name in ("document_payload", "document_payload_sha256", "gate_report"):
                    vvalues.pop(name, None)
            version = upsert(versions_run, Version, source_id, target=vtarget, values=vvalues, created_at=timestamp((document or {}).get("issuedAt") or row.get("createdAt")))
            if document is not None:
                primary_tier = vvalues["tier"]
                _snapshot(run, BomSnapshot, version, primary_tier, True, row.get("bomSnapshotId"), bom_snapshots.get(row.get("bomSnapshotId")), qid)
                _snapshot(run, CommercialSnapshot, version, primary_tier, True, row.get("commercialSnapshotId"), commercial_snapshots.get(row.get("commercialSnapshotId")), qid)
                for option in row.get("alternativeOptions") or []:
                    tier = PLATFORM_TIER.get(option.get("tier"), None)
                    if tier is None or tier == primary_tier:
                        run.violation(qid, "invalid_alternative", f"Alternative option {option.get('tier')!r} skipped.")
                        continue
                    _snapshot(run, BomSnapshot, version, tier, False, option.get("bomSnapshotId"), bom_snapshots.get(option.get("bomSnapshotId")), qid)
                    _snapshot(run, CommercialSnapshot, version, tier, False, option.get("commercialSnapshotId"), commercial_snapshots.get(option.get("commercialSnapshotId")), qid)
            current = version
        if current is not None and quotation.current_version_id != current.pk:
            Quotation.all_objects.filter(pk=quotation.pk).update(current_version=current)

    result = run_import(run, rows, import_row, user=user, dry_run=True if dry_run else False, action=ACTION, object_type="quotations.quotation", namespaces=(CACHE_NAMESPACE,))
    orphans = sorted((set(bom_snapshots) | set(commercial_snapshots)) - referenced)
    for snapshot_id in orphans:
        result["violations"].append({"source_table": "quotation-state.json", "source_id": snapshot_id, "code": "orphan_snapshot", "message": "No quotation refers to this snapshot; not imported."})
    return result


def import_sent_quotes(rows: list[dict], *, user=None, dry_run: bool = False) -> dict:
    """Main backend ``sent_quotes`` → ``quotations_email_log`` legacy rows (no version: the website quote had none);
    ``send_quote_junk`` is not migrated. ``quote_id`` is the idempotency key (``legacy_ref``); every imported row is
    also linked in ``core_legacy_map`` (``BACKEND sent_quotes <id>``) so the migration's row-count check sees it."""
    run = ImportRun(BACKEND, "sent_quotes")
    with transaction.atomic():
        for row in sorted(rows, key=lambda item: item.get("id") or 0):
            quote_id = str(row.get("quote_id") or "").strip()
            if not quote_id:
                run.violation(row.get("id"), "incomplete_row", "A sent quote needs quote_id; skipped.")
                continue
            values = {
                "channel": EmailChannel.LEGACY_LINK,
                "to": str(row.get("phone") or "")[:254],
                "name": str(row.get("name") or "")[:100],
                "status": EmailStatus.SENT if row.get("is_sent") else EmailStatus.QUEUED,
                "legacy_url": str(row.get("quote_url") or "")[:200],
                "created_at": timestamp(row.get("created_at")) or timezone.now(),
                "sent_at": (timestamp(row.get("updated_at")) or timezone.now()) if row.get("is_sent") else None,
            }
            existing = EmailLog.objects.filter(legacy_ref=quote_id).first()
            if existing is None:
                existing = EmailLog.objects.create(legacy_ref=quote_id, **values)
                run.created += 1
            elif any(getattr(existing, name) != value for name, value in values.items()):
                EmailLog.objects.filter(pk=existing.pk).update(**values)
                run.updated += 1
            else:
                run.skipped += 1
            if row.get("id") is not None:
                run.link(row["id"], existing)  # core_legacy_map: verify_migration #1 accounts for every source row
        record(
            ACTION,
            object_type="quotations.emaillog",
            actor=user,
            after={"source_table": run.source_table, "rows": len(rows), "checksum": checksum(rows), **{**run.as_dict(), "violations": len(run.violations)}},
        )
        if dry_run:
            transaction.set_rollback(True)
        else:
            bump(CACHE_NAMESPACE)
    return run.as_dict()
