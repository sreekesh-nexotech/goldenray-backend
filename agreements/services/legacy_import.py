"""Legacy import into ``agreements_agreement`` (PLAN §7.5 "Purchase Agreement"; Plan 2 §3.1 "Migration") — called by
``migrations_tools`` (``import_pa``) with the ``flarize_agr`` JSON export of each browser profile (``crs``, ``admin``).

Contract (:mod:`customers.services.import_support`): plain rows in, ``{"created", "updated", "skipped", "violations"}``
out; idempotent through ``core_legacy_map`` (``PA flarize_agr <profile>/<record id>``); violations are listed, never
raised; ``dry_run`` rolls back; one audit row per call.

Each record ``{id, type, typeName, customerName, createdAt, data}`` becomes one ISSUED agreement with ``legacy = true``:

* ``type`` 1/2/3 → PURCHASE_AGREEMENT / SALE_ORDER / EXTRA_STRUCTURE; ``number`` = ``<PROFILE>-<record id>`` (the page
  numbered nothing; D2-2 keeps legacy identifiers on imported rows), ``issued_at`` = ``createdAt``;
* the customer is matched by phone (``match_or_create_by_phone``, source PA_IMPORT); a record without a usable phone
  (Sale Order and Extra Structure forms have no phone field) gets its own customer, remembered in the legacy map so a
  re-run finds it again (``customer_without_phone``);
* typed columns are parsed from ``data`` (size, hybrid detection, phase, equipment labels, DCR, wattage, prices, KSEB
  fee); a value that does not parse is left empty and reported (``unparsed_value``); nothing is re-priced;
* ``payload`` = the document the templates print, built from those columns, plus ``legacy_record`` = the raw record,
  frozen with its SHA-256. A record already imported unchanged is skipped (its frozen payload is never rebuilt);
* ``uid`` = ``uuid5(SI_AGREEMENT_NAMESPACE, "PA:<record id>")`` (``site_inspections.services.legacy_import.agreement_uid``),
  the reference a legacy PA site inspection carries, so imported inspections link without a lookup (a record id seen
  in both profiles keeps it for the first import only: ``duplicate_record_id``);
* the page's built-in demo records (``seed()``: ids ``a1`` … ``a5``) are not migrated (``demo_record_not_migrated``).

:func:`report_pa_catalog` compares the page's Upstash catalog with the platform masters and lists the differences;
it creates nothing.
"""

from __future__ import annotations

import datetime as dt
import re
import uuid
from decimal import Decimal

from agreements.models import Agreement, AgreementKind, AgreementStatus, InverterType, Language, Phase, SystemType, Variant
from agreements.services import document, fees
from agreements.services.common import CACHE_NAMESPACE, MAX_MONEY, money
from core.models import LegacyMap
from customers.services.import_support import ImportRun, date_value, mapped_id, run_import, timestamp, upsert
from engines.frozen import sha256_hex

PA = LegacyMap.SourceSystem.PA
SOURCE_TABLE = "flarize_agr"
CUSTOMER_TABLE = "flarize_agr.customer"
ACTION = "agreements.legacy_import"
QUOTE_PREFIX = "QUO-GR-AS-26-"
DEMO_IDS = frozenset({"a1", "a2", "a3", "a4", "a5"})
KINDS = {1: AgreementKind.PURCHASE_AGREEMENT, 2: AgreementKind.SALE_ORDER, 3: AgreementKind.EXTRA_STRUCTURE}
PHASES = {"single phase": Phase.ONE, "3 phase": Phase.THREE}
INVERTER_TYPES = {"string inverter": InverterType.STRING, "micro inverter": InverterType.MICRO, "hybrid inverter": InverterType.HYBRID}
VARIANTS = {"base": Variant.BASE, "value": Variant.VALUE, "premium": Variant.PREMIUM}
KW_RE = re.compile(r"(?P<kw>\d+(?:\.\d+)?)\s*KW", re.IGNORECASE)
WATT_RE = re.compile(r"(?P<w>\d{3,4})\s*W", re.IGNORECASE)
HYBRID_RE = re.compile(r"hybrid", re.IGNORECASE)


def _text(value, limit: int) -> str:
    return str(value if value is not None else "").strip()[:limit]


class _Parser:
    def __init__(self, run: ImportRun, source_id: str, data: dict):
        self.run, self.source_id, self.data = run, source_id, data

    def text(self, key: str, limit: int) -> str:
        return _text(self.data.get(key), limit)

    def money(self, key: str) -> Decimal | None:
        raw = self.text(key, 64)
        if not raw:
            return None
        value = money(raw)
        if value is None or value < 0 or value > MAX_MONEY:
            self.run.violation(self.source_id, "unparsed_value", f"{key}={raw!r} is not an amount; left empty.")
            return None
        return value

    def choice(self, key: str, mapping: dict, default=""):
        raw = self.text(key, 64)
        if not raw:
            return default
        value = mapping.get(raw.lower())
        if value is None:
            self.run.violation(self.source_id, "unparsed_value", f"{key}={raw!r} is not a known value; left empty.")
            return default
        return value

    def yes(self, key: str) -> bool:
        return self.text(key, 8).upper() == "YES"


def _columns(run: ImportRun, source_id: str, kind: str, data: dict) -> dict:
    parse = _Parser(run, source_id, data)
    size = parse.text("kw", 120)
    match = KW_RE.search(size)
    capacity = Decimal(match.group("kw")) if match else None
    if size and capacity is None:
        run.violation(source_id, "unparsed_value", f"kw={size!r} names no capacity; capacity_kw left empty.")
    inverter_type = parse.choice("invtype", INVERTER_TYPES)
    panel = parse.text("panel", 255)
    panel_capacity = parse.text("panelcap", 64)
    watt = WATT_RE.search(panel_capacity)
    battery = parse.text("battery", 64)
    values = {
        "system_type": SystemType.HYBRID if HYBRID_RE.search(size) or inverter_type == InverterType.HYBRID else SystemType.ON_GRID,
        "capacity_kw": capacity if capacity and capacity > 0 else None,
        "size_label": size,
        "phase": parse.choice("phase", PHASES),
        "variant": parse.choice("variant", VARIANTS),
        "panel_label": panel,
        "panel_capacity_label": panel_capacity,
        "panel_capacity_w": int(watt.group("w")) if watt else None,
        "panel_dcr": True if panel.upper().endswith("- DCR") else False if panel.upper().endswith("- NDCR") else None,
        "inverter_brand": parse.text("inverter", 100),
        "inverter_type": inverter_type,
        "battery_label": "" if battery.lower() == "none" else battery,
        "structure_material": parse.text("struct", 120),
        "extra_structure": parse.yes("extra"),
        "walkway_required": parse.yes("walkway"),
        "ladder_required": parse.yes("lader"),
        "add_on_offer": parse.text("addoffer", 2000),
        "extra_description": parse.text("extdesc", 2000),
        "legacy_quotation_ref": f"{QUOTE_PREFIX}{parse.text('quoteno', 32)}" if parse.text("quoteno", 32) else "",
    }
    extra = parse.money("extcost") or Decimal("0.00")
    if kind == AgreementKind.PURCHASE_AGREEMENT:
        original, discount, final = parse.money("origprice"), parse.money("discount") or Decimal("0.00"), parse.money("total")
    else:
        original = parse.money("amt" if kind == AgreementKind.SALE_ORDER else "total")
        discount = Decimal("0.00")
        final = money(original + extra) if original is not None and original + extra <= MAX_MONEY else None
    values.update(original_price=original, extra_cost=extra, discount=discount, final_price=final)
    fee = parse.money("kseb") if kind != AgreementKind.EXTRA_STRUCTURE else None
    row = fees.by_amount(fee)
    values.update(statutory_fee_amount=fee, statutory_fee_id=row.pk if row else None, statutory_fee_label=row.label if row else ("KSEB registration" if fee else ""))
    return values


def _issued_at(value):
    """``createdAt`` (ISO timestamp, or a bare date on the page's demo records) → aware datetime, else ``None``."""
    try:
        return timestamp(value)
    except ValueError:
        pass
    try:
        day = date_value(value)
    except ValueError:
        return None
    return dt.datetime.combine(day, dt.time(), tzinfo=dt.UTC) if day else None


def _customer(run: ImportRun, source_id: str, record: dict, data: dict, user):
    from customers.models import Customer
    from customers.services.customers import ensure_customer
    from customers.services.legacy_import import match_or_create_by_phone

    name = _text(data.get("name") or record.get("customerName"), 255) or "Unknown"
    address = _text(data.get("address"), 10_000)
    phone = _text(data.get("phone"), 32)
    if phone:
        customer, created = match_or_create_by_phone(phone=phone, name=name, user=user, source=Customer.Source.PA_IMPORT, values={"address": address} if address else None)
        if customer is not None:
            if created:
                run.violation(source_id, "customer_created", f"No customer with {phone!r}; created {customer.code}.")
            return customer
        run.violation(source_id, "unparsed_value", f"phone={phone!r} is not a phone number.")
    known = mapped_id(PA, CUSTOMER_TABLE, source_id)
    customer = Customer.all_objects.filter(pk=known).first() if known else None
    if customer is None:
        customer, _ = ensure_customer(user=user, values={"name": name, "address": address}, source=Customer.Source.PA_IMPORT)
        run.violation(source_id, "customer_without_phone", f"The record has no usable phone; created {customer.code} for {name!r} (merge it if it is a known customer).")
        LegacyMap.objects.update_or_create(source_system=PA, source_table=CUSTOMER_TABLE, source_id=source_id, defaults={"target_table": customer._meta.db_table, "target_id": customer.pk})
    return customer


def _modified_on_platform(run: ImportRun, source_id: str, agreement: Agreement) -> bool:
    """True when the platform wrote the imported agreement after its last import (accepted, cancelled, superseded, a
    revision drafted, …). The import stamps ``updated_at`` from ``createdAt`` and links the legacy map afterwards, so an
    imported row's ``updated_at`` never passes its map's ``imported_at``; every platform write (``versioned_update``)
    stamps now. A re-run must never take such an agreement back to the browser record (e.g. ACCEPTED → ISSUED)."""
    if agreement.status != AgreementStatus.ISSUED or agreement.deleted_at is not None:
        return True
    if Agreement.all_objects.filter(supersedes=agreement).exists():
        return True
    imported_at = LegacyMap.objects.filter(source_system=run.source_system, source_table=run.source_table, source_id=source_id, target_id=agreement.pk).values_list("imported_at", flat=True).first()
    return imported_at is not None and agreement.updated_at > imported_at


def _new_uid(run: ImportRun, source_id: str, record_id: str, uid_for) -> uuid.UUID:
    """The uid of a newly imported agreement: ``uid_for(record id)`` when the caller gives one (``migrations_tools``
    passes the site-inspection link rule, so a legacy inspection's ``agreement_uid`` finds it), unless another agreement
    already holds that uid (the same record id in the other profile: ``duplicate_record_id``, a random uid)."""
    wanted = uid_for(record_id) if uid_for is not None else None
    if wanted is None:
        return uuid.uuid4()
    if Agreement.all_objects.filter(uid=wanted).exists():
        run.violation(source_id, "duplicate_record_id", f"Record id {record_id!r} was already imported from another profile; this copy gets its own uid (inspections link to the first).")
        return uuid.uuid4()
    return wanted


def _record(run: ImportRun, row: dict, *, profile: str, user, company: dict, uid_for=None) -> None:
    record = row["record"]
    record_id = _text(record.get("id"), 64)
    source_id = f"{profile}/{record_id}"
    kind = KINDS.get(record.get("type")) if isinstance(record.get("type"), int) else None
    data = record.get("data")
    if not record_id or kind is None or not isinstance(data, dict):
        run.violation(source_id, "invalid_record", "A record needs an id, a type 1/2/3 and a data object; skipped.")
        return
    if record_id in DEMO_IDS:
        run.violation(source_id, "demo_record_not_migrated", "The page's built-in demo record (seed()) is not migrated.")
        return
    issued_at = _issued_at(record.get("createdAt"))
    if issued_at is None:
        run.violation(source_id, "invalid_record", f"createdAt={record.get('createdAt')!r} is not a date; skipped.")
        return
    target = run.find_target(Agreement, source_id)
    if target is not None and (target.payload or {}).get("legacy_record") == record:
        run.skipped += 1
        run.link(source_id, target)
        return
    if target is not None and _modified_on_platform(run, source_id, target):
        run.skipped += 1
        run.violation(source_id, "modified_on_platform", f"Agreement {target.number} changed on the platform after it was imported ({target.status}); left as it is.")
        return
    if target is not None:
        run.violation(source_id, "legacy_record_changed", "The browser record changed since the last import; its agreement was rebuilt.")
    customer = _customer(run, source_id, record, data, user)
    values = {
        "kind": kind,
        "customer_id": customer.pk,
        "status": AgreementStatus.ISSUED,
        "number": f"{profile.upper()}-{record_id}"[:48],
        "language": Language.EN,
        "issued_at": issued_at,
        "legacy": True,
        "legacy_ref": source_id[:96],
        **_columns(run, source_id, kind, data),
    }
    agreement = Agreement(
        uid=target.uid if target is not None else _new_uid(run, source_id, record_id, uid_for), customer=customer, **{key: value for key, value in values.items() if key != "customer_id"}
    )
    payload = {**document.build(agreement, company=company), "legacy_record": record}
    values.update(payload=payload, payload_sha256=sha256_hex(payload))
    if target is None:
        values["uid"] = agreement.uid
    upsert(run, Agreement, source_id, target=target, values=values, created_at=issued_at, updated_at=issued_at)


def import_pa_agreements(records: list[dict], *, profile: str, user=None, dry_run: bool = False, uid_for=None) -> dict:
    """Import one browser profile's ``flarize_agr`` export (``profile`` = ``crs`` or ``admin``), oldest first.

    ``uid_for(record id) -> UUID`` fixes the uid of a newly imported agreement. It defaults to
    ``site_inspections.services.legacy_import.agreement_uid`` — ``uuid5(SI_AGREEMENT_NAMESPACE, "PA:<id>")``, the key a
    legacy PA inspection carries — so imported inspections link without a lookup."""
    if uid_for is None:
        from site_inspections.services.legacy_import import agreement_uid as uid_for
    profile = re.sub(r"[^a-z0-9_-]", "", (profile or "").lower())[:16] or "pa"
    company = document.company_block()
    rows = [{"record": record, "created": str(record.get("createdAt") or "") if isinstance(record, dict) else ""} for record in records or []]

    def import_row(run: ImportRun, row: dict) -> None:
        if not isinstance(row["record"], dict):
            run.violation("?", "invalid_record", "A record must be an object; skipped.")
            return
        _record(run, row, profile=profile, user=user, company=company, uid_for=uid_for)

    return run_import(
        ImportRun(PA, SOURCE_TABLE), rows, import_row, user=user, dry_run=dry_run, action=ACTION, object_type="agreements.agreement", namespaces=(CACHE_NAMESPACE,), order=lambda row: row["created"]
    )


# ── Upstash catalog report ───────────────────────────────────────────────────────────────────────────────────────


def _values(doc: dict, key: str) -> list[str]:
    items = doc.get(key) or []
    return [str(item.get("value") if isinstance(item, dict) else item).strip() for item in items if (item.get("value") if isinstance(item, dict) else item)]


def report_pa_catalog(doc: dict) -> dict:
    """The page's catalog (``panels_dcr``, ``panels_ndcr``, ``inverters``, ``batteries``, ``kseb``, …) against the platform
    masters: every entry without a platform counterpart is listed (``not_in_catalog``); nothing is created."""
    from catalog.models import Component

    run = ImportRun(PA, "catalog")
    brands = {name.lower() for name in Component.objects.exclude(brand_label="").values_list("brand_label", flat=True)}
    for key in ("panels_dcr", "panels_ndcr", "inverters"):
        for value in _values(doc, key):
            brand = value.split(" - ")[0].strip().lower()
            if brand in brands:
                run.skipped += 1
            else:
                run.violation(f"{key}:{value}", "not_in_catalog", f"{value!r}: no catalog component of brand {brand!r}.")
    for value in _values(doc, "kseb"):
        label, _, amount = value.rpartition("|")
        match = KW_RE.search(label)
        fee = fees.current(phase="", capacity_kw=Decimal(match.group("kw"))) if match else None
        if fee is not None and fee.amount == money(amount):
            run.skipped += 1
        else:
            run.violation(f"kseb:{value}", "not_in_catalog", f"{value!r}: no current KSEB registration fee of that band and amount.")
    for key in ("plants", "capacities", "batteries", "structures", "invtypes"):
        for value in _values(doc, key):
            run.violation(f"{key}:{value}", "listed_only", f"{value!r} ({key}) has no master table; agreements keep it as printed text.")
    return run.as_dict()
