"""Legacy imports into pricing (PLAN §7.3, §7.4; contract in :mod:`pricing.services.import_support`).

Main backend (``GoldenApp``, source system BACKEND):

* :func:`import_prices` — the price rows the catalog importers return (``bom_catalogitem.price``/``per_watt``, website
  ``batteries.battery_price``, Flarize ``catalog.json`` ``price``/``perWatt``, battery-master prices): one LIST (or
  PURCHASE) ``pricing_price`` row per component, resolved through ``core_legacy_map`` from the catalog import;
* :func:`import_bom_global_costs` — ``bom_globalcosts`` → one ``pricing_cost_config`` row per column;
* :func:`import_bom_market_rates` — ``bom_marketrate.size_rates`` exploded per size into the set "Imported <date>";
* :func:`import_bom_offers` — ``bom_offer`` → ``pricing_offer`` (ACTIVE when ``active``, else ARCHIVED).

Flarize (FLARIZE): :func:`import_flarize_pricing` (``catalog.json`` ``costs``, ``transportConfig``, ``officeExpense``,
``installationMatrix``, ``marketRates``, ``offers``) and :func:`import_flarize_documents` (``cost-config.json``,
``project-rate-card.json``, ``quotation-policy.json``, the four customer-engine configurations). Purchase Agreement
(PA): :func:`import_pa_kseb_fees` (the ``kseb`` fee list → ``pricing_statutory_fee``). Procurement batches, the price
master and the commercial history are imported by ``procurement.services.legacy_import``.

**D-2 — Flarize wins.** Where both sources describe the same thing (a component's LIST price, a global cost, a market
rate cell, an offer code) the Flarize value is kept in either import order, and every difference is reported as a
``d2_flarize_wins`` warning with both values. Ownership is read from ``core_legacy_map`` (both sources map onto the
row they describe), so a re-run of the main-backend import never overwrites a value Flarize asserted.

Column mapping: docs/decisions/pricing-procurement.md.
"""

from __future__ import annotations

import json
import re
from collections.abc import Iterable
from decimal import Decimal

from core.models import actor_or_none
from core.services import stamp_create
from pricing.models import (
    CostConfig,
    InstallationMatrix,
    InstallationType,
    MarketRate,
    MarketRateSet,
    MarketRateSetStatus,
    Offer,
    OfferStatus,
    OfferSystem,
    OfferTier,
    OfferTransition,
    OfferType,
    Price,
    PriceKind,
    PriceSource,
    StatutoryFee,
    StatutoryFeeKind,
    SystemType,
    Tier,
    ValidityKind,
    ValidityPolicy,
    ValidityWindowStatus,
)
from pricing.services import cost_config, market_rates
from pricing.services.common import AUTHORING_NAMESPACE, today, valid_size_key
from pricing.services.import_support import (
    BACKEND,
    FLARIZE,
    PA,
    SOURCE_RANK,
    BadValue,
    ImportRun,
    component_for,
    day,
    dec,
    guarded,
    legacy_user,
    mapped,
    mapped_from,
    moment,
    remember,
    run,
    set_timestamps,
)
from pricing.services.prices import current_row, write_price

ACTION = "pricing.legacy_import"
IMPORTED_SET_TABLE = "pricing.imported_market_rate_set"
SYSTEMS = {"ongrid": SystemType.ONGRID, "hybrid": SystemType.HYBRID, "upgrade": SystemType.UPGRADE}
TIERS = {"base": Tier.BASE, "value": Tier.VALUE, "premium": Tier.PREMIUM}
OFFER_SYSTEMS = {"all": OfferSystem.ALL, "ongrid": OfferSystem.ONGRID, "hybrid": OfferSystem.HYBRID, "upgrade": OfferSystem.UPGRADE}
OFFER_TIERS = {"all": OfferTier.ALL, "base": OfferTier.BASE, "value": OfferTier.VALUE, "premium": OfferTier.PREMIUM}
OFFER_TYPES = {"flat": OfferType.FLAT, "percent": OfferType.PERCENT, "percentage": OfferType.PERCENT}
MARKET_KEY_RE = re.compile(r"^(?P<system>ongrid|hybrid)_(?P<tier>base|value|premium)(?:_(?P<bat>[0-9]))?(?:_up(?P<future>[0-9.]+(?:sp|tp)?))?(?:_(?P<variant>[A-Za-z][A-Za-z0-9]*))?$")
UPGRADE_KEY_RE = re.compile(r"^upgrade_(?P<from>[0-9.]+(?:sp|tp)?)_(?P<to>[0-9.]+(?:sp|tp)?)$")

# bom_globalcosts column → (cost config key, conversion)
GLOBAL_COSTS = {
    "install_rate": ("install_rate", "money"),
    "service_rate_year": ("service_rate_year", "money"),
    "service_years": ("service_years", "integer"),
    "transport_rate": ("transport_rate_per_km", "money"),
    "default_dist_km": ("transport_base_km", "integer"),
    "miscellaneous": ("miscellaneous", "money"),
    "office": ("office_per_project", "money"),
    "elevated_structure_rate": ("elevated_structure_rate", "money"),
    "sheet_structure_rate": ("sheet_structure_rate", "money"),
    "gp_rate_per_kg": ("gp_rate_per_kg", "money"),
    "gi_rate_per_kg": ("gi_rate_per_kg", "money"),
    "structure_labor": ("structure_labor", "money"),
    "structure_repair_pct": ("structure_repair_pct", "percent"),
}
# catalog.json section → {field: (cost config key, conversion)}
FLARIZE_COSTS = {
    "costs": {
        "installRate": ("install_rate", "money"),
        "serviceRateYear": ("service_rate_year", "money"),
        "serviceYears": ("service_years", "integer"),
        "transportRate": ("transport_rate_per_km", "money"),
        "defaultDistKm": ("transport_base_km", "integer"),
        "miscellaneous": ("miscellaneous", "money"),
        "office": ("office_per_project", "money"),
        "elevatedStructureRate": ("elevated_structure_rate", "money"),
        "sheetStructureRate": ("sheet_structure_rate", "money"),
        "gpRatePerKg": ("gp_rate_per_kg", "money"),
        "giRatePerKg": ("gi_rate_per_kg", "money"),
        "structureLabor": ("structure_labor", "money"),
        "structureRepairPct": ("structure_repair_pct", "percent"),
    },
    "transportConfig": {"baseDistanceKm": ("transport_base_km", "integer"), "costPerKm": ("transport_extra_rate_per_km", "money")},
    "officeExpense": {"monthlyExpense": ("office_expense_monthly", "money"), "expectedProjectsPerMonth": ("expected_projects_per_month", "integer")},
}
DOCUMENT_KEYS = {"energy": "energy.config", "savings": "savings.config", "subsidy": "subsidy.config", "finance": "finance.config"}


def _config_value(value, conversion: str):
    if conversion == "percent":
        number = dec(value, "percent", places=6, digits=12)
        return None if number is None else cost_config.json_number((number / 100).normalize())
    if conversion == "integer":
        number = dec(value, "integer", places=0, digits=12)
        return None if number is None else int(number)
    number = dec(value, "amount", places=2, digits=14)
    return None if number is None else cost_config.json_number(number)


def _same_value(a, b) -> bool:
    return json.dumps(a, sort_keys=True, default=str) == json.dumps(b, sort_keys=True, default=str)


# ── prices ─────────────────────────────────────────────────────────────────────────────────────────────────────────


def _price_owner(row: Price) -> int:
    """How strongly a current row holds its price: platform-authored rows (manual, batch) beat every import."""
    if row.source in (PriceSource.MANUAL, PriceSource.BATCH):
        return 3
    return max((SOURCE_RANK.get(system, 0) for system in mapped_from(row, source_table_prefix="price:")), default=0)


def import_prices(prices: Iterable[dict], *, user=None, dry_run: bool = False, effective_from=None) -> dict:
    """The catalog importers' price rows → ``pricing_price`` (LIST, or PURCHASE from the battery master).

    Each row: ``{sku, kind, amount, per_watt, source_system, source_table, source_id}``. The component comes from
    ``core_legacy_map`` (the catalog import mapped the source row), else by SKU. D-2 decides between sources; a
    current MANUAL/BATCH price (platform data) is never replaced by an import (reported ``platform_price_kept``).
    """
    rows = [dict(row) for row in prices or []]

    def body(result: ImportRun) -> None:
        for row in rows:
            table = f"price:{row.get('source_table')}:{row.get('kind')}"
            guarded(result, table, row.get("source_id"), lambda row=row, table=table: _import_price(result, row, table, user=user, effective_from=effective_from))

    return run("prices", ACTION, rows, body, user=user, dry_run=dry_run, object_type="pricing.price", namespaces=(AUTHORING_NAMESPACE,))


def _import_price(result: ImportRun, row: dict, table: str, *, user, effective_from) -> str:
    system, source_id, kind = row.get("source_system"), row.get("source_id"), row.get("kind")
    if kind not in (PriceKind.LIST, PriceKind.PURCHASE, PriceKind.LANDED):
        raise BadValue(f"kind={kind!r}: not a price kind")
    amount = dec(row.get("amount"), "amount")
    if amount is None:
        return "skipped"
    if amount < 0:
        raise BadValue(f"amount={amount}: negative")
    per_watt = dec(row.get("per_watt"), "per_watt", places=4, digits=10)
    component = component_for(system, row.get("source_table"), source_id, row.get("sku"))
    if component is None:
        result.violation(table, source_id, "component_not_found", f"No catalog component for {row.get('sku')!r}; import the catalog first.", sku=row.get("sku"))
        return "skipped"
    gst_inclusive = row.get("source_table") == "batteries"  # the website's battery price is a customer price
    current = current_row(component, kind)
    if current is not None:
        owner = _price_owner(current)
        incoming = SOURCE_RANK.get(system, 0)
        same = current.amount == amount and current.per_watt == per_watt and current.gst_inclusive == gst_inclusive
        if incoming < owner:
            if not same:
                code = "platform_price_kept" if owner == 3 else "d2_flarize_wins"
                result.violation(
                    table,
                    source_id,
                    code,
                    f"{component.sku} {kind}: kept {current.amount} ({current.source_ref or current.source}); {system} has {amount}.",
                    severity="warning",
                    sku=component.sku,
                    kept=str(current.amount),
                    other=str(amount),
                )
            remember(system, table, source_id, current)
            return "skipped"
        if same:
            remember(system, table, source_id, current)
            return "skipped"
        if incoming > owner and owner:
            result.violation(
                table,
                source_id,
                "d2_flarize_wins",
                f"{component.sku} {kind}: {system} {amount} replaces {current.amount}.",
                severity="warning",
                sku=component.sku,
                kept=str(amount),
                other=str(current.amount),
            )
    new, _ = write_price(
        component,
        kind,
        amount,
        user=user,
        source=PriceSource.IMPORT,
        effective_from=effective_from or today(),
        source_ref=f"{system}:{row.get('source_table')}#{source_id}",
        per_watt=per_watt,
        gst_inclusive=gst_inclusive,
        note=f"Imported from {system} {row.get('source_table')}",
    )
    remember(system, table, source_id, new)
    return "updated" if current is not None else "created"


# ── cost configuration ─────────────────────────────────────────────────────────────────────────────────────────────


def _import_config_value(result: ImportRun, key: str, value, *, system: str, table: str, source_id: str, user, effective_from, created_at=None) -> str:
    value = cost_config.validate_value(key, value)
    current = CostConfig.objects.filter(key=key, effective_to__isnull=True).first()
    if current is not None:
        owners = mapped_from(current)
        if system == BACKEND and FLARIZE in owners:
            if not _same_value(current.value, value):
                result.violation(
                    table, source_id, "d2_flarize_wins", f"{key}: kept Flarize {current.value}; the main backend has {value}.", severity="warning", key=key, kept=current.value, other=value
                )
            remember(system, table, source_id, current)
            return "skipped"
        if _same_value(current.value, value):
            remember(system, table, source_id, current)
            return "skipped"
        if system == FLARIZE and BACKEND in owners:
            result.violation(table, source_id, "d2_flarize_wins", f"{key}: Flarize {value} replaces the main backend's {current.value}.", severity="warning", key=key, kept=value, other=current.value)
    row = cost_config.set_value(
        key, value, user=user, effective_from=effective_from or today(), source_ref=f"{system}:{table}"[:64], note=f"Imported from {system} {table}", created_at=created_at, audit=False
    )
    remember(system, table, source_id, row)
    return "updated" if current is not None else "created"


def import_bom_global_costs(rows: Iterable[dict], *, user=None, dry_run: bool = False, effective_from=None) -> dict:
    """``bom_globalcosts`` (singleton; the legacy calculator reads ``.first()``) → one cost-config row per column."""
    rows = sorted((dict(row) for row in rows or []), key=lambda row: int(row["id"]))

    def body(result: ImportRun) -> None:
        for extra in rows[1:]:
            result.violation("bom_globalcosts", extra["id"], "extra_global_costs_row", "The legacy calculator reads only the first row; this one is ignored.", severity="warning")
            result.count("bom_globalcosts", "skipped")
        if not rows:
            return
        row = rows[0]
        created_at = moment(row.get("updated_at"))
        for column, (key, conversion) in GLOBAL_COSTS.items():
            if column not in row:
                continue
            guarded(
                result,
                "bom_globalcosts",
                f"{row['id']}:{column}",
                lambda column=column, key=key, conversion=conversion: _import_config_value(
                    result,
                    key,
                    _config_value(row[column], conversion),
                    system=BACKEND,
                    table="bom_globalcosts",
                    source_id=f"{row['id']}:{column}",
                    user=user,
                    effective_from=effective_from,
                    created_at=created_at,
                ),
            )

    return run("bom_globalcosts", ACTION, rows, body, user=user, dry_run=dry_run, object_type="pricing.costconfig", namespaces=(AUTHORING_NAMESPACE,))


# ── market rates ───────────────────────────────────────────────────────────────────────────────────────────────────


def _imported_set(result: ImportRun, system: str, *, user) -> MarketRateSet | None:
    rate_set = None
    for source in (BACKEND, FLARIZE):
        found = mapped(source, IMPORTED_SET_TABLE, "default", MarketRateSet)
        if found is not None and found.deleted_at is None:
            rate_set = found
            break
    if rate_set is None:
        name, counter = f"Imported {today().isoformat()}", 2
        while MarketRateSet.objects.filter(name__iexact=name).exists():
            name, counter = f"Imported {today().isoformat()} ({counter})", counter + 1
        rate_set = market_rates.create_set(user=user, data={"name": name, "note": "Legacy market rates: bom_marketrate + Flarize catalog.json marketRates (Flarize wins on conflicts, D-2)."})
    remember(system, IMPORTED_SET_TABLE, "default", rate_set)
    if rate_set.status != MarketRateSetStatus.DRAFT:
        result.violation(IMPORTED_SET_TABLE, "default", "market_rate_set_locked", f"The imported set {rate_set.name!r} is {rate_set.status}; market rates were not re-imported.")
        return None
    return rate_set


def _upsert_rate(result: ImportRun, rate_set: MarketRateSet, values: dict, *, system: str, table: str, source_id: str, user, created_at=None) -> str:
    clean, errors = market_rates.normalise_rate(values, values.get("sort_order", 0))
    if errors:
        raise BadValue("; ".join(f"{field}: {', '.join(messages)}" for field, messages in errors.items()))
    lookup = {name: clean[name] for name in ("system_type", "tier", "battery_config", "size_key", "from_size_key", "future_size_key", "variant")}
    existing = MarketRate.objects.filter(set=rate_set, **lookup).first()
    if existing is not None:
        owners = mapped_from(existing)
        if system == BACKEND and FLARIZE in owners:
            if existing.customer_price_incl_gst != clean["customer_price_incl_gst"]:
                result.violation(
                    table,
                    source_id,
                    "d2_flarize_wins",
                    f"{market_rates.rate_key(existing)} {existing.size_key}: kept Flarize {existing.customer_price_incl_gst}; the main backend has {clean['customer_price_incl_gst']}.",
                    severity="warning",
                    kept=str(existing.customer_price_incl_gst),
                    other=str(clean["customer_price_incl_gst"]),
                )
            remember(system, table, source_id, existing)
            return "skipped"
        if system == FLARIZE and BACKEND in owners and existing.customer_price_incl_gst != clean["customer_price_incl_gst"]:
            result.violation(
                table,
                source_id,
                "d2_flarize_wins",
                f"{market_rates.rate_key(existing)} {existing.size_key}: Flarize {clean['customer_price_incl_gst']} replaces the main backend's {existing.customer_price_incl_gst}.",
                severity="warning",
                kept=str(clean["customer_price_incl_gst"]),
                other=str(existing.customer_price_incl_gst),
            )
        diff = {name: clean[name] for name in ("customer_price_incl_gst", "sort_order") if getattr(existing, name) != clean[name]}
        if diff:
            existing.versioned_update(user, **diff)
        remember(system, table, source_id, existing)
        return "updated" if diff else "skipped"
    rate = MarketRate(set=rate_set, **clean)
    stamp_create(rate, user)
    rate.save()
    set_timestamps(rate, created_at=created_at, updated_at=created_at)
    remember(system, table, source_id, rate)
    return "created"


def import_bom_market_rates(rows: Iterable[dict], *, user=None, dry_run: bool = False) -> dict:
    """``bom_marketrate`` → the DRAFT set "Imported <date>", one row per ``size_rates`` entry (key order kept)."""
    rows = sorted((dict(row) for row in rows or []), key=lambda row: int(row["id"]))

    def body(result: ImportRun) -> None:
        rate_set = _imported_set(result, BACKEND, user=user)
        if rate_set is None:
            return
        for row in rows:
            _bom_market_rate(result, rate_set, row, user=user)
        rate_set.versioned_update(user)

    return run("bom_marketrate", ACTION, rows, body, user=user, dry_run=dry_run, object_type="pricing.marketrate", namespaces=(AUTHORING_NAMESPACE,))


def _bom_market_rate(result: ImportRun, rate_set: MarketRateSet, row: dict, *, user) -> None:
    table = "bom_marketrate"
    system = SYSTEMS.get(row.get("system_type"))
    raw_tier = row.get("tier") or ""
    tier = TIERS.get(raw_tier) if raw_tier else ""
    if system is None or tier is None:
        result.violation(table, row["id"], "unknown_value", f"system_type={row.get('system_type')!r} tier={row.get('tier')!r} is not a known combination.")
        result.count(table, "skipped")
        return
    if system != SystemType.UPGRADE and (row.get("from_size") or row.get("to_size")):
        # No home: only UPGRADE rows have from/to sizes. Imported as a plain row it would silently replace the cells
        # of the row the calculator really reads (the on-grid lookup even skips rows with a from_size).
        result.violation(
            table,
            row["id"],
            "upgrade_sizes_on_non_upgrade",
            f"A {row.get('system_type')} row with from_size={row.get('from_size')!r} to_size={row.get('to_size')!r} is not imported; only upgrade rows carry these sizes.",
        )
        result.count(table, "skipped")
        return
    rates = row.get("size_rates") or {}
    if isinstance(rates, str):
        rates = json.loads(rates)
    if not rates:
        result.violation(table, row["id"], "empty_size_rates", "No sizes: nothing to import (the calculator reads 0 for every size).", severity="warning")
        result.count(table, "skipped")
        return
    created_at = moment(row.get("updated_at"))
    if system == SystemType.UPGRADE:
        cells = [(key, {"size_key": str(row.get("to_size") or ""), "from_size_key": str(row.get("from_size") or ""), "value": value}) for key, value in rates.items()]
    else:
        cells = [(key, {"size_key": str(key), "battery_config": row.get("bat_config") or "", "value": value}) for key, value in rates.items()]
    for index, (key, cell) in enumerate(cells):
        source_id = f"{row['id']}:{key}"
        if system == SystemType.UPGRADE and key != "rate":
            result.violation(table, source_id, "unknown_upgrade_key", f"Upgrade rates carry only 'rate'; {key!r} is ignored.")
            result.count(table, "skipped")
            continue
        value = cell.pop("value")
        if value is None:
            result.violation(table, source_id, "null_rate", "A null rate is stored as 0 (not set), as the legacy calculator reads it.", severity="warning")
            value = 0
        values = {"system_type": system, "tier": tier, "sort_order": index, **cell}
        guarded(
            result,
            table,
            source_id,
            lambda values=values, value=value, source_id=source_id: _upsert_rate(
                result, rate_set, {**values, "customer_price_incl_gst": dec(value, "size_rates")}, system=BACKEND, table=table, source_id=source_id, user=user, created_at=created_at
            ),
        )


def _flarize_market_rates(result: ImportRun, market: dict, *, user) -> None:
    table = "catalog.json:marketRates"
    rate_set = _imported_set(result, FLARIZE, user=user)
    if rate_set is None:
        return
    for key, sizes in (market or {}).items():
        upgrade = UPGRADE_KEY_RE.match(key)
        match = MARKET_KEY_RE.match(key)
        if not upgrade and not match:
            result.violation(table, key, "unknown_market_rate_key", f"{key!r} is not a market-rate key (<sys>_<tier>[_<bat>][_up<size>]).")
            result.count(table, "skipped")
            continue
        if not sizes:
            result.violation(table, key, "empty_size_rates", f"{key}: no sizes (nothing to import).", severity="warning")
            result.count(table, "skipped")
            continue
        for index, (size, value) in enumerate(sizes.items()):
            source_id = f"{key}:{size}"
            if upgrade:
                if size != "rate":
                    result.violation(table, source_id, "unknown_upgrade_key", f"Upgrade rates carry only 'rate'; {size!r} is ignored.")
                    result.count(table, "skipped")
                    continue
                values = {"system_type": SystemType.UPGRADE, "tier": "", "size_key": upgrade.group("to"), "from_size_key": upgrade.group("from")}
            else:
                values = {
                    "system_type": SYSTEMS[match.group("system")],
                    "tier": TIERS[match.group("tier")],
                    "battery_config": match.group("bat") or "",
                    "size_key": size,
                    "future_size_key": match.group("future") or "",
                    "variant": match.group("variant") or "",
                }
            values["sort_order"] = index
            guarded(
                result,
                table,
                source_id,
                lambda values=values, value=value, source_id=source_id: _upsert_rate(
                    result, rate_set, {**values, "customer_price_incl_gst": dec(value if value is not None else 0, "marketRates")}, system=FLARIZE, table=table, source_id=source_id, user=user
                ),
            )
    rate_set.versioned_update(user)


# ── offers ─────────────────────────────────────────────────────────────────────────────────────────────────────────

OFFER_FIELDS = ("name", "type", "value", "applies_to_system", "applies_to_tier", "applies_to_size_key", "applies_to_size_kw", "starts_on", "ends_on", "description", "content_version")


def _upsert_offer(result: ImportRun, values: dict, *, system: str, table: str, source_id: str, user, created_at=None, transitions: list[dict] | None = None) -> str:
    code = values["code"]
    existing = mapped(system, table, source_id, Offer) or Offer.objects.filter(code__iexact=code).first()
    status = values.pop("status")
    if existing is not None:
        owners = mapped_from(existing)
        if system == BACKEND and FLARIZE in owners:
            differing = sorted(name for name in OFFER_FIELDS if name in values and getattr(existing, name) != values[name])
            if differing:
                result.violation(table, source_id, "d2_flarize_wins", f"Offer {code}: kept Flarize values for {', '.join(differing)}.", severity="warning", fields=differing)
            remember(system, table, source_id, existing)
            return "skipped"
        diff = {name: values[name] for name in OFFER_FIELDS if name in values and getattr(existing, name) != values[name]}
        if system == FLARIZE and BACKEND in owners and diff:
            result.violation(table, source_id, "d2_flarize_wins", f"Offer {code}: Flarize values replace the main backend's for {', '.join(sorted(diff))}.", severity="warning", fields=sorted(diff))
        if existing.status != status:
            diff["status"] = status
            OfferTransition.objects.create(offer=existing, from_status=existing.status, to_status=status, by_label=system, reason=f"Imported from {system} {table}")
        if diff:
            existing.versioned_update(user, **diff)
        _import_transitions(existing, transitions or [], system=system)
        remember(system, table, source_id, existing)
        return "updated" if diff else "skipped"
    duplicates = Offer.objects.filter(name__iexact=values["name"]).exclude(code__iexact=code)
    if duplicates.exists():
        result.violation(table, source_id, "possible_duplicate_offer", f"Offer {code} has the same name as {', '.join(o.code for o in duplicates)}; both kept.", severity="warning")
    offer = Offer(status=status, status_changed_at=created_at, **values)
    stamp_create(offer, user)
    offer.save()
    set_timestamps(offer, created_at=created_at, updated_at=created_at)
    if transitions:
        _import_transitions(offer, transitions, system=system)
    else:
        OfferTransition.objects.create(offer=offer, from_status="", to_status=status, at=created_at or offer.created_at, by_label=system, reason=f"Imported from {system} {table}")
    remember(system, table, source_id, offer)
    return "created"


def _import_transitions(offer: Offer, transitions: list[dict], *, system: str) -> None:
    for item in transitions:
        at = moment(item.get("changedAt")) or offer.created_at
        to_status = item.get("to")
        if to_status not in OfferStatus.values or OfferTransition.objects.filter(offer=offer, at=at, to_status=to_status).exists():
            continue
        by = legacy_user(system, item.get("changedBy"))
        OfferTransition.objects.create(
            offer=offer,
            from_status=item.get("from") or "",
            to_status=to_status,
            at=at,
            by=actor_or_none(by),
            by_label="" if by else str(item.get("changedBy") or "")[:64],
            reason=item.get("reason") or "",
        )


def import_bom_offers(rows: Iterable[dict], *, user=None, dry_run: bool = False) -> dict:
    """``bom_offer`` → ``pricing_offer``: ACTIVE while ``active`` (the calculator also checks the dates), else ARCHIVED."""
    rows = sorted((dict(row) for row in rows or []), key=lambda row: int(row["id"]))

    def body(result: ImportRun) -> None:
        for row in rows:
            guarded(result, "bom_offer", row["id"], lambda row=row: _bom_offer(result, row, user=user))

    return run("bom_offer", ACTION, rows, body, user=user, dry_run=dry_run, object_type="pricing.offer", namespaces=(AUTHORING_NAMESPACE,))


def _bom_offer(result: ImportRun, row: dict, *, user) -> str:
    offer_type = OFFER_TYPES.get(row.get("offer_type"))
    system = OFFER_SYSTEMS.get(row.get("applies_to"))
    tier = OFFER_TIERS.get(row.get("applies_to_tier"))
    if offer_type is None or system is None or tier is None:
        raise BadValue(f"offer_type={row.get('offer_type')!r} applies_to={row.get('applies_to')!r} applies_to_tier={row.get('applies_to_tier')!r}: unknown value")
    value = dec(row.get("value"), "value", digits=12)
    if offer_type == OfferType.PERCENT and value > 100:
        raise BadValue(f"value={value}: a percentage offer above 100 %")
    values = {
        "code": str(row["offer_id"]).strip(),
        "name": str(row.get("name") or "").strip(),
        "type": offer_type,
        "value": value,
        "applies_to_system": system,
        "applies_to_tier": tier,
        "applies_to_size_key": "",
        "applies_to_size_kw": None,
        "starts_on": day(row.get("start_date")),
        "ends_on": day(row.get("end_date")),
        "status": OfferStatus.ACTIVE if row.get("active") else OfferStatus.ARCHIVED,
    }
    return _upsert_offer(result, values, system=BACKEND, table="bom_offer", source_id=str(row["id"]), user=user, created_at=moment(row.get("created_at")))


def _flarize_offer(result: ImportRun, item: dict, *, user) -> str:
    offer_type = OFFER_TYPES.get(item.get("type"))
    system = OFFER_SYSTEMS.get(item.get("appliesTo") or "all")
    tier = OFFER_TIERS.get(item.get("appliesToTier") or "all")
    status = item.get("status") or ("ACTIVE" if item.get("active") else "DRAFT")
    if offer_type is None or system is None or tier is None or status not in OfferStatus.values:
        raise BadValue(f"type={item.get('type')!r} appliesTo={item.get('appliesTo')!r} appliesToTier={item.get('appliesToTier')!r} status={status!r}: unknown value")
    size_key = "" if (item.get("appliesToSize") or "all") == "all" else str(item["appliesToSize"])
    parsed = valid_size_key(size_key) if size_key else None
    if size_key and parsed is None:
        raise BadValue(f"appliesToSize={size_key!r}: not a size key")
    values = {
        "code": str(item["id"]),
        "name": str(item.get("name") or "").strip(),
        "type": offer_type,
        "value": dec(item.get("value"), "value", digits=12),
        "applies_to_system": system,
        "applies_to_tier": tier,
        "applies_to_size_key": size_key,
        "applies_to_size_kw": parsed[0] if parsed else None,
        "starts_on": day(item.get("startDate")),
        "ends_on": day(item.get("endDate")),
        "description": item.get("description") or "",
        "content_version": int(item.get("offerVersion") or 1),
        "status": status,
    }
    return _upsert_offer(
        result, values, system=FLARIZE, table="catalog.json:offers", source_id=str(item["id"]), user=user, created_at=moment(item.get("createdAt")), transitions=item.get("transitions") or []
    )


# ── Flarize catalog.json ───────────────────────────────────────────────────────────────────────────────────────────


def import_flarize_pricing(catalog_json: dict, *, user=None, dry_run: bool = False, effective_from=None) -> dict:
    """Flarize ``catalog.json``: ``costs``/``transportConfig``/``officeExpense`` → cost config, ``installationMatrix``,
    ``marketRates`` (into the imported set), ``offers``. ``_legacyMarkers`` are ignored (PLAN §7.4)."""
    catalog_json = catalog_json or {}

    def body(result: ImportRun) -> None:
        seen: dict[str, tuple[str, object]] = {}
        for section, fields in FLARIZE_COSTS.items():
            data = catalog_json.get(section) or {}
            for field, (key, conversion) in fields.items():
                if field not in data:
                    continue
                source_id = f"{section}.{field}"
                table = f"catalog.json:{section}"
                try:
                    value = _config_value(data[field], conversion)
                except BadValue as exc:
                    result.violation(table, source_id, "invalid_value", str(exc))
                    result.count(table, "skipped")
                    continue
                if key in seen and not _same_value(seen[key][1], value):
                    result.violation(table, source_id, "flarize_conflict", f"{key}: {seen[key][0]} says {seen[key][1]}, {source_id} says {value}; the first is kept.", severity="warning")
                    result.count(table, "skipped")
                    continue
                seen[key] = (source_id, value)
                guarded(
                    result,
                    table,
                    source_id,
                    lambda key=key, value=value, table=table, source_id=source_id: _import_config_value(
                        result, key, value, system=FLARIZE, table=table, source_id=source_id, user=user, effective_from=effective_from
                    ),
                )
        _installation_matrix(result, catalog_json.get("installationMatrix") or {}, user=user)
        _flarize_market_rates(result, catalog_json.get("marketRates") or {}, user=user)
        for item in catalog_json.get("offers") or []:
            guarded(result, "catalog.json:offers", item.get("id"), lambda item=item: _flarize_offer(result, item, user=user))

    return run("catalog.json pricing", ACTION, [catalog_json], body, user=user, dry_run=dry_run, object_type="pricing.costconfig", namespaces=(AUTHORING_NAMESPACE,))


ROOF_KEYS = {"flat": InstallationType.FLAT, "sheet": InstallationType.SHEET, "elevated": InstallationType.ELEVATED}


def _installation_matrix(result: ImportRun, matrix: dict, *, user) -> None:
    table = "catalog.json:installationMatrix"
    for size, roofs in matrix.items():
        parsed = valid_size_key(size)
        for roof, cost in (roofs or {}).items():
            source_id = f"{size}:{roof}"
            if parsed is None or roof not in ROOF_KEYS:
                result.violation(table, source_id, "unknown_value", f"{size!r} × {roof!r} is not a size key × flat/sheet/elevated.")
                result.count(table, "skipped")
                continue
            guarded(result, table, source_id, lambda parsed=parsed, roof=roof, cost=cost, source_id=source_id: _matrix_cell(parsed, ROOF_KEYS[roof], cost, source_id=source_id, user=user))


def _matrix_cell(parsed, installation_type: str, cost, *, source_id: str, user) -> str:
    size_kw, phase = parsed
    amount = dec(cost, "installationMatrix")
    row = mapped(FLARIZE, "catalog.json:installationMatrix", source_id, InstallationMatrix)
    if row is None or row.deleted_at is not None:
        row = InstallationMatrix.objects.filter(size_kw=size_kw, phase=phase, installation_type=installation_type).first()
    if row is None:
        row = InstallationMatrix(size_kw=size_kw, phase=phase, installation_type=installation_type, install_cost=amount)
        stamp_create(row, user)
        row.save()
        outcome = "created"
    elif row.install_cost != amount:
        row.versioned_update(user, install_cost=amount)
        outcome = "updated"
    else:
        outcome = "skipped"
    remember(FLARIZE, "catalog.json:installationMatrix", source_id, row)
    return outcome


# ── Flarize configuration documents ────────────────────────────────────────────────────────────────────────────────


def import_flarize_documents(
    *,
    cost_config_json: dict | None = None,
    rate_card_json: dict | None = None,
    quotation_policy_json: dict | None = None,
    engine_configs: dict[str, dict] | None = None,
    user=None,
    dry_run: bool = False,
    effective_from=None,
) -> dict:
    """Flarize configuration files → cost-config documents (+ GST and margin keys) and the validity policy.

    ``cost-config.json`` → ``cost_engine.config`` (whole) and ``gst_*`` / ``target_gross_margin_by_tier`` (fractions);
    ``project-rate-card.json`` → ``rate_card``; ``quotation-policy.json`` → the QUOTATION validity default + windows;
    ``engine_configs`` ``{"energy"|"savings"|"subsidy"|"finance": document}`` → ``<name>.config``.
    """
    payloads = [cost_config_json, rate_card_json, quotation_policy_json, engine_configs]

    def document(result, key, value, table):
        guarded(result, table, "document", lambda: _import_config_value(result, key, value, system=FLARIZE, table=table, source_id="document", user=user, effective_from=effective_from))

    def body(result: ImportRun) -> None:
        if cost_config_json:
            document(result, "cost_engine.config", cost_config_json, "cost-config.json")
            gst = cost_config_json.get("gst") or {}
            for field, key in (("goodsValuationPct", "gst_goods_share"), ("goodsRatePct", "gst_goods_rate"), ("serviceValuationPct", "gst_services_share"), ("serviceRatePct", "gst_services_rate")):
                if gst.get(field) is not None:
                    value = _config_value(gst[field], "percent")
                    guarded(
                        result,
                        "cost-config.json:gst",
                        field,
                        lambda key=key, value=value, field=field: _import_config_value(
                            result, key, value, system=FLARIZE, table="cost-config.json:gst", source_id=field, user=user, effective_from=effective_from
                        ),
                    )
            tiers = ((cost_config_json.get("margin") or {}).get("tiers")) or {}
            if tiers:
                margin = {}
                for tier, entry in tiers.items():
                    margin[tier] = {"target": _config_value(entry.get("targetMarginPct"), "percent")}
                    if entry.get("minimumMarginPct") is not None:
                        margin[tier]["minimum"] = _config_value(entry["minimumMarginPct"], "percent")
                guarded(
                    result,
                    "cost-config.json:margin",
                    "tiers",
                    lambda: _import_config_value(
                        result, "target_gross_margin_by_tier", margin, system=FLARIZE, table="cost-config.json:margin", source_id="tiers", user=user, effective_from=effective_from
                    ),
                )
        if rate_card_json:
            document(result, "rate_card", rate_card_json, "project-rate-card.json")
        for name, value in (engine_configs or {}).items():
            if name not in DOCUMENT_KEYS:
                result.violation("engine-config", name, "unknown_document", f"{name!r} is not energy, savings, subsidy or finance.")
                continue
            document(result, DOCUMENT_KEYS[name], value, f"{name}-config.json")
        if quotation_policy_json:
            _quotation_policy(result, quotation_policy_json, user=user)

    return run("Flarize configuration", ACTION, payloads, body, user=user, dry_run=dry_run, object_type="pricing.costconfig", namespaces=(AUTHORING_NAMESPACE,))


def _quotation_policy(result: ImportRun, policy_json: dict, *, user) -> None:
    table = "quotation-policy.json"
    default = policy_json.get("validity") or {}
    if default.get("defaultDays") is not None:
        guarded(result, table, "validity", lambda: _validity_default(default, user=user))
    for item in policy_json.get("policies") or []:
        guarded(result, f"{table}:policies", item.get("policyId"), lambda item=item: _validity_window(item, user=user))


def _validity_default(default: dict, *, user) -> str:
    days = int(dec(default["defaultDays"], "defaultDays", places=0, digits=4))
    values = {
        "days": days,
        "version_label": str(default.get("version") or "")[:80],
        "note": default.get("note") or "",
        "effective_from": moment(default.get("updatedAt")) or moment("2000-01-01T00:00:00Z"),
    }
    policy = ValidityPolicy.objects.filter(key="QUOTATION", kind=ValidityKind.DEFAULT).first()
    if policy is None:
        policy = ValidityPolicy(key="QUOTATION", kind=ValidityKind.DEFAULT, **values)
        stamp_create(policy, legacy_user(FLARIZE, default.get("updatedBy")) or user)
        policy.save()
        outcome = "created"
    else:
        diff = {name: value for name, value in values.items() if getattr(policy, name) != value}
        if diff:
            policy.versioned_update(user, **diff)
        outcome = "updated" if diff else "skipped"
    remember(FLARIZE, "quotation-policy.json", "validity", policy)
    return outcome


def _validity_window(item: dict, *, user) -> str:
    policy_id = str(item.get("policyId") or "").strip()
    if not policy_id:
        raise BadValue("policyId: missing")
    start, end = moment(item.get("effectiveFrom")), moment(item.get("effectiveTo"))
    if start is None or (end is not None and end <= start):
        raise BadValue(f"{policy_id}: the window must start before it ends")
    status = item.get("status") or ValidityWindowStatus.ACTIVE
    if status not in ValidityWindowStatus.values:
        raise BadValue(f"status={status!r}: ACTIVE or INACTIVE")
    values = {
        "days": int(dec(item.get("validityDays"), "validityDays", places=0, digits=4)),
        "effective_from": start,
        "effective_to": end,
        "status": status,
        "version_label": str(item.get("version") or "")[:80],
        "note": item.get("note") or "",
    }
    window = ValidityPolicy.objects.filter(key="QUOTATION", kind=ValidityKind.WINDOW, policy_id=policy_id).first()
    if window is None:
        window = ValidityPolicy(key="QUOTATION", kind=ValidityKind.WINDOW, policy_id=policy_id, **values)
        stamp_create(window, legacy_user(FLARIZE, item.get("createdBy")) or user)
        window.save()
        created_at = moment(item.get("createdAt"))
        set_timestamps(window, created_at=created_at, updated_at=created_at)
        outcome = "created"
    else:
        diff = {name: value for name, value in values.items() if getattr(window, name) != value}
        if diff:
            window.versioned_update(user, **diff)
        outcome = "updated" if diff else "skipped"
    remember(FLARIZE, "quotation-policy.json:policies", policy_id, window)
    return outcome


# ── Purchase Agreement KSEB fees ───────────────────────────────────────────────────────────────────────────────────

KW_RE = re.compile(r"(?P<kw>\d+(?:\.\d+)?)\s*KW", re.IGNORECASE)


def import_pa_kseb_fees(entries: Iterable, *, user=None, dry_run: bool = False, effective_from=None) -> dict:
    """The Purchase Agreement ``kseb`` list → KSEB_REGISTRATION statutory fees (one per capacity band).

    Entries are the page's built-in options (``{"v": "5400", "l": "3 KW — ₹ 5,400"}``) or the Upstash strings
    ``"<label>|<fee>"``. The capacity in the label (``3 KW``) becomes ``capacity_kw_max``.
    """
    entries = list(entries or [])

    def body(result: ImportRun) -> None:
        for index, entry in enumerate(entries):
            if isinstance(entry, str):
                label, _, fee = entry.rpartition("|")
            else:
                label, fee = str(entry.get("l") or ""), entry.get("v")
            guarded(result, "kseb", label or index, lambda label=label, fee=fee: _kseb_fee(label, fee, user=user, effective_from=effective_from))

    return run("kseb", ACTION, entries, body, user=user, dry_run=dry_run, object_type="pricing.statutoryfee", namespaces=(AUTHORING_NAMESPACE,))


def _kseb_fee(label: str, fee, *, user, effective_from) -> str:
    match = KW_RE.search(label or "")
    if match is None:
        raise BadValue(f"{label!r}: no capacity (e.g. '3 KW') in the label")
    capacity = Decimal(match.group("kw"))
    amount = dec(str(fee).replace(",", "").strip(), "fee", digits=12)
    short_label = label.split("—")[0].strip() or label.strip()
    current = StatutoryFee.objects.filter(kind=StatutoryFeeKind.KSEB_REGISTRATION, phase__isnull=True, capacity_kw_max=capacity, effective_to__isnull=True).first()
    if current is None:
        current = StatutoryFee(kind=StatutoryFeeKind.KSEB_REGISTRATION, label=short_label, capacity_kw_max=capacity, amount=amount, effective_from=effective_from or today())
        stamp_create(current, user)
        current.save()
        outcome = "created"
    elif current.amount != amount or current.label != short_label:
        current.versioned_update(user, amount=amount, label=short_label)
        outcome = "updated"
    else:
        outcome = "skipped"
    remember(PA, "kseb", label, current)
    return outcome
