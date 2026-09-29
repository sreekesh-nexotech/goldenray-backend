"""Legacy import of the Flarize pack configuration (PLAN §7.4 ``pack-config.json``) and the first releases.

* :func:`import_flarize_pack_config` — ``pack-config.json`` (``approved``, ``draft``, ``history``) → ``packs_config_version``:
  every ``history`` entry older than the approved one → a SUPERSEDED version without configuration (Flarize kept only
  the metadata), ``approved`` → the APPROVED version (its configuration, approver, submitter, note), ``draft`` → the open
  DRAFT/SUBMITTED version (``basedOn``, configuration, ``changeLog``, submission/rejection metadata). The Flarize
  package registry (``packages.proposed.json``) supplies the per-pack pins: for each pack of an imported configuration,
  the registry package ``buildBom`` would read (the first with the same system type, system size, tier and phase)
  pins the components of the template's variable slots — so the platform resolves every BOM exactly as Flarize did.
* :func:`publish_initial_releases` — PLAN §7.4 "then publish PriceRelease #1 and PackRelease #1 through the services":
  the approved configuration's market rates (the ones Flarize priced with) are merged into the market-rate set
  (``pack_config_market_rate_wins`` warnings where the imported set said otherwise), the set is activated, and both
  releases are published through ``pricing.services.releases.publish`` / ``packs.services.releases.publish``. Returns
  the PackRelease publish report (PLAN §7.6 #8: the BLOCK findings are listed for review).

Contract (PLAN §7.1): plain dicts in, ``{"created", "updated", "skipped", "violations", "counts"}`` out, idempotent through
``core_legacy_map`` (``FLARIZE pack-config.json:versions <number>``; re-running updates, never duplicates), ``dry_run``
rolls everything back, one ``packs.legacy_import`` audit row per call. Run after the catalog, pricing and bom imports.
"""

from __future__ import annotations

import copy

from django.utils import timezone

from catalog.models import Component
from core.errors import Conflict, DomainError
from engines.pack_config import PackConfigError, validate_config
from packs.models import OPEN_STATUSES, ConfigPack, ConfigPin, ConfigStatus, ConfigVersion
from packs.services import engine, mirror
from packs.services.context import engine_context
from pricing.services.import_support import FLARIZE, BadValue, ImportRun, legacy_user, mapped, moment, remember, run, set_timestamps

ACTION = "packs.legacy_import"
VERSION_TABLE = "pack-config.json:versions"
IMPORTED_SET_TABLE = "pricing.imported_market_rate_set"


def _registry_package(registry: dict, spec: engine.PackSpec) -> dict | None:
    for package in (registry or {}).get("packages") or []:
        if package.get("systemType") == spec.system and package.get("size") == spec.system_size and package.get("tier") == spec.tier and package.get("phase") == spec.phase:
            return package
    return None


def _actor(result: ImportRun, source_id, value):
    """``(user, legacy label)`` for a Flarize user id: the imported user when the users import mapped it."""
    if not value:
        return None, ""
    user = legacy_user(FLARIZE, value)
    return user, "" if user is not None else str(value)[:64]


def _upsert_version(result: ImportRun, number: int, values: dict, *, user, created_at=None) -> tuple[ConfigVersion | None, str]:
    table = VERSION_TABLE
    existing = mapped(FLARIZE, table, number, ConfigVersion)
    if existing is None:
        clash = ConfigVersion.all_objects.filter(number=number).first()
        if clash is not None:
            result.violation(table, number, "number_taken", f"v{number} already exists in the platform (not from this import); the Flarize version was not imported.")
            return None, "skipped"
        version = ConfigVersion(number=number, **values)
        version.created_by = version.updated_by = user if getattr(user, "pk", None) else None
        version.save()
        set_timestamps(version, created_at=created_at, updated_at=created_at)
        remember(FLARIZE, table, number, version)
        return version, "created"
    if existing.deleted_at is not None:
        result.violation(table, number, "target_deleted", f"v{number} was deleted in the platform; not re-created.", severity="warning")
        return None, "skipped"
    wanted = values.get("status")
    if existing.status in (ConfigStatus.PUBLISHED, ConfigStatus.SUPERSEDED) and wanted == ConfigStatus.APPROVED:
        values = {**values, "status": existing.status}  # published or superseded in the platform since: never downgrade
    elif existing.status != wanted and not (existing.status in OPEN_STATUSES and wanted in OPEN_STATUSES):
        result.violation(table, number, "target_moved_on", f"v{number} is {existing.status} in the platform (the source says {wanted}); kept.", severity="warning")
        return existing, "skipped"
    changed = {name: value for name, value in values.items() if _column(existing, name) != _column_value(existing, name, value)}
    if changed:
        existing.versioned_update(user, **changed)
        return existing, "updated"
    return existing, "skipped"


def _column(instance, name: str):
    field = instance._meta.get_field(name)
    return getattr(instance, field.attname)


def _column_value(instance, name: str, value):
    field = instance._meta.get_field(name)
    return value.pk if field.is_relation and value is not None else value


def _history(result: ImportRun, store: dict, approved_number, *, user) -> None:
    for entry in store.get("history") or []:
        number = entry.get("version")
        if not isinstance(number, int) or number < 1:
            result.violation(VERSION_TABLE, number, "invalid_value", f"history version {number!r} is not a positive integer.")
            result.count(VERSION_TABLE, "skipped")
            continue
        if number == approved_number:
            continue  # the approved version is imported from store["approved"] (it carries the configuration)
        approver, label = _actor(result, number, entry.get("approvedBy"))
        at = moment(entry.get("approvedAt"))
        values = {
            "status": ConfigStatus.SUPERSEDED,
            "config": None,
            "note": str(entry.get("note") or ""),
            "approved_by": approver,
            "approved_at": at,
            "legacy_actor": label,
            "change_log": [
                {
                    "at": entry.get("approvedAt"),
                    "by": entry.get("approvedBy"),
                    "section": "*",
                    "note": f"Flarize history: {entry.get('changes', 0)} change-log entries",
                    "changed": True,
                    "legacy": True,
                }
            ],
        }
        _, outcome = _upsert_version(result, number, values, user=user, created_at=at)
        result.count(VERSION_TABLE, outcome)


def _validated(result: ImportRun, number, config) -> dict | None:
    try:
        validate_config(config)
    except PackConfigError as exc:
        result.violation(VERSION_TABLE, number, "invalid_config", exc.message)
        return None
    return copy.deepcopy(config)


def _pins(result: ImportRun, version: ConfigVersion, registry: dict, *, user) -> int:
    """Pins of every pack of ``version`` from the registry package ``buildBom`` reads for it (replaces earlier pins).
    Returns the number of pins written (created, changed or removed): 0 when a re-run finds them as they were."""
    config = version.config or {}
    slots = {system: {slot.get("category") for slot in (template or {}).get("slots") or [] if slot.get("variable", True)} for system, template in (config.get("bomTemplates") or {}).items()}
    packs = {pack.key: pack for pack in ConfigPack.objects.filter(config_version=version)}
    skus = {c.sku: c for c in Component.objects.all().only("id", "sku")}
    count = 0
    for spec in engine.enumerate_packs(config):
        pack = packs.get(spec.key)
        package = _registry_package(registry, spec)
        if pack is None or package is None:
            continue
        wanted = {}
        for item in package.get("components") or []:
            role = item.get("role")
            sku = item.get("componentId") or item.get("pinnedComponentId")
            if role not in slots.get(spec.system, set()) or not sku or role in wanted:
                continue
            component = skus.get(sku)
            if component is None:
                result.violation(
                    "packages.proposed.json:components",
                    f"{package.get('packageId')}:{role}",
                    "unknown_component",
                    f"{spec.key}: registry component {sku!r} is not in the catalog; not pinned.",
                    severity="warning",
                )
                continue
            wanted[role] = (component, item.get("derivedBy") == "explicit", [str(a) for a in item.get("approvedAlternates") or []])
        current = {pin.slot_key: pin for pin in ConfigPin.objects.filter(pack=pack)}
        for slot_key, pin in current.items():
            if slot_key not in wanted:
                pin.soft_delete(user)
                count += 1
        for slot_key, (component, authoritative, alternates) in wanted.items():
            pin = current.get(slot_key)
            if pin is None:
                pin = ConfigPin(pack=pack, slot_key=slot_key, component=component, authoritative=authoritative, alternates=alternates)
                pin.created_by = pin.updated_by = user if getattr(user, "pk", None) else None
                pin.save()
                count += 1
            elif (pin.component_id, pin.authoritative, pin.alternates) != (component.pk, authoritative, alternates):
                pin.versioned_update(user, component=component, authoritative=authoritative, alternates=alternates)
                count += 1
    return count


def import_flarize_pack_config(store: dict, registry: dict | None = None, *, user=None, dry_run: bool = False) -> dict:
    """``pack-config.json`` (+ ``packages.proposed.json`` for the pins) → ``packs_config_version`` rows."""
    store = store or {}

    def body(result: ImportRun) -> None:
        approved = store.get("approved") or {}
        draft = store.get("draft") or {}
        approved_number = approved.get("version")
        _history(result, store, approved_number, user=user)
        ctx = engine_context()
        imported = []
        if approved:
            config = _validated(result, approved_number, approved.get("config"))
            if config is not None:
                approver, label = _actor(result, approved_number, approved.get("approvedBy"))
                submitter, _ = _actor(result, approved_number, approved.get("submittedBy"))
                at = moment(approved.get("approvedAt"))
                values = {
                    "status": ConfigStatus.APPROVED,
                    "config": config,
                    "note": str(approved.get("note") or ""),
                    "approved_by": approver,
                    "approved_at": at,
                    "submitted_by": submitter,
                    "submitted_at": moment(approved.get("submittedAt")),
                    "legacy_actor": label,
                }
                version, outcome = _upsert_version(result, approved_number, values, user=user, created_at=at)
                result.count(VERSION_TABLE, outcome)
                if version is not None:
                    imported.append(version)
        if draft:
            number = draft.get("version")
            config = _validated(result, number, draft.get("config"))
            if config is not None:
                based_on = mapped(FLARIZE, VERSION_TABLE, draft.get("basedOn"), ConfigVersion) if draft.get("basedOn") is not None else None
                status = ConfigStatus.SUBMITTED if draft.get("status") == "SUBMITTED" else ConfigStatus.DRAFT
                submitter, _ = _actor(result, number, draft.get("submittedBy"))
                rejecter, _ = _actor(result, number, draft.get("rejectedBy"))
                values = {
                    "status": status,
                    "config": config,
                    "based_on": based_on,
                    "change_log": list(draft.get("changeLog") or []),
                    "submitted_by": submitter,
                    "submitted_at": moment(draft.get("submittedAt")),
                    "rejected_by": rejecter,
                    "rejected_at": moment(draft.get("rejectedAt")),
                    "rejection_reason": str(draft.get("rejectionReason") or ""),
                }
                version, outcome = _upsert_version(result, number, values, user=user, created_at=moment(draft.get("updatedAt")))
                result.count(VERSION_TABLE, outcome)
                if version is not None and version.status in (ConfigStatus.DRAFT, ConfigStatus.SUBMITTED, ConfigStatus.APPROVED):
                    imported.append(version)
        for version in imported:
            version.refresh_from_db()
            mirror.mirror(version, user=user, ctx=ctx)
            if registry:
                pins = _pins(result, version, registry, user=user)
                result.count("packages.proposed.json:pins", "updated" if pins else "skipped")
                mirror.mirror(version, user=user, ctx=ctx)

    try:
        return run("pack-config.json", ACTION, [store, registry], body, user=user, dry_run=dry_run, object_type="packs.configversion", namespaces=("packs:authoring",))
    except BadValue as exc:
        raise DomainError("invalid_input", str(exc)) from None


# ── PriceRelease #1 and PackRelease #1 ─────────────────────────────────────────────────────────────────────────────


def _pack_config_rates(result: ImportRun, config: dict) -> dict[tuple, dict]:
    from pricing.services import market_rates
    from pricing.services.legacy_import import MARKET_KEY_RE, SYSTEMS, TIERS, UPGRADE_KEY_RE

    rows = {}
    for key, sizes in (config.get("marketRates") or {}).items():
        upgrade, match = UPGRADE_KEY_RE.match(key), MARKET_KEY_RE.match(key)
        if not upgrade and not match:
            result.violation("pack-config.json:marketRates", key, "unknown_market_rate_key", f"{key!r} is not a market-rate key.")
            continue
        for index, (size, value) in enumerate((sizes or {}).items()):
            if upgrade:
                if size != "rate":
                    continue
                values = {"system_type": "UPGRADE", "tier": "", "size_key": upgrade.group("to"), "from_size_key": upgrade.group("from")}
            else:
                values = {
                    "system_type": SYSTEMS[match.group("system")],
                    "tier": TIERS[match.group("tier")],
                    "battery_config": match.group("bat") or "",
                    "size_key": size,
                    "future_size_key": match.group("future") or "",
                    "variant": match.group("variant") or "",
                }
            values["customer_price_incl_gst"] = value if value is not None else 0
            values["sort_order"] = index
            clean, errors = market_rates.normalise_rate(values, index)
            if errors:
                result.violation("pack-config.json:marketRates", f"{key}:{size}", "invalid_value", "; ".join(f"{f}: {', '.join(m)}" for f, m in errors.items()))
                continue
            rows[market_rates.natural_key(clean)] = clean
    return rows


def _market_rate_set(result: ImportRun, version: ConfigVersion, *, user):
    """The ACTIVE market-rate set holding the approved configuration's rates (created/activated when needed)."""
    from pricing.models import MarketRateSet, MarketRateSetStatus
    from pricing.services import market_rates

    wanted = _pack_config_rates(result, version.config)
    active = market_rates.active_set()

    def rows_of(rate_set):
        return (
            {
                market_rates.natural_key({name: getattr(rate, name) for name in market_rates.RATE_FIELDS}): {name: getattr(rate, name) for name in market_rates.RATE_FIELDS}
                for rate in market_rates.rates_of(rate_set)
            }
            if rate_set
            else {}
        )

    if active is not None:
        current = rows_of(active)
        if all(key in current and current[key]["customer_price_incl_gst"] == row["customer_price_incl_gst"] for key, row in wanted.items()):
            return active, "unchanged"
    imported = None
    for system in (FLARIZE, "BACKEND"):
        found = mapped(system, IMPORTED_SET_TABLE, "default", MarketRateSet)
        if found is not None and found.deleted_at is None:
            imported = found
            break
    if imported is not None and imported.status == MarketRateSetStatus.DRAFT:
        target = imported
    else:
        base = name = f"Flarize pack config v{version.number}"
        counter = 2
        while MarketRateSet.objects.filter(name__iexact=name).exists():
            name, counter = f"{base} ({counter})", counter + 1
        target = market_rates.create_set(user=user, data={"name": name, "note": "Approved pack configuration market rates (the rates Flarize priced with).", "copy_from": active or imported})
    merged = rows_of(target)
    for key, row in wanted.items():
        before = merged.get(key)
        if before is not None and before["customer_price_incl_gst"] != row["customer_price_incl_gst"]:
            result.violation(
                "pack-config.json:marketRates",
                "/".join(str(part) for part in key if part),
                "pack_config_market_rate_wins",
                f"{key}: the approved pack configuration's {row['customer_price_incl_gst']} replaces {before['customer_price_incl_gst']}.",
                severity="warning",
                kept=str(row["customer_price_incl_gst"]),
                other=str(before["customer_price_incl_gst"]),
            )
        merged[key] = {**(before or {}), **row, "sort_order": before["sort_order"] if before else row["sort_order"]}
    market_rates.put_rates(target, user=user, rows=[{name: value for name, value in row.items()} for row in merged.values()])
    target.refresh_from_db()
    if target.status != MarketRateSetStatus.ACTIVE:
        market_rates.activate_set(target, user=user, note="Legacy import: approved pack configuration market rates.")
    return target, "activated"


def publish_initial_releases(*, user=None, note: str = "Legacy import (PLAN §7.4)") -> dict:
    """Publish PriceRelease #1 and PackRelease #1 from the imported data; returns the PackRelease publish report.

    Idempotent: when nothing changed since the current releases (``RELEASE_UNCHANGED``) they are reused.
    """
    from django.db import transaction

    from packs.services import releases as pack_releases
    from packs.services.versions import current_version
    from pricing.services import releases as price_releases

    result = ImportRun("initial releases")
    version = current_version()
    if version is None:
        raise Conflict("no_approved_version", "Import the Flarize pack configuration first (no approved version).")
    with transaction.atomic():
        rate_set, rates_outcome = _market_rate_set(result, version, user=user)
        price_preview = price_releases.preview()
        blockers = [item for item in price_preview["items"] if item["severity"] == "BLOCK"]
        if blockers and not all(item["code"] == "RELEASE_UNCHANGED" for item in blockers):
            raise Conflict("publish_blocked", "PriceRelease #1 cannot be published.", errors={"report": [f"{i['code']}: {i['message']}" for i in blockers]})
        price_release = price_releases.publish(user=user, note=note) if not blockers else price_releases.current_release()
        pack_preview = pack_releases.preview()
        blockers = [item for item in pack_preview["items"] if item["severity"] == "BLOCK"]
        if blockers and not all(item["code"] == "RELEASE_UNCHANGED" for item in blockers):
            raise Conflict("publish_blocked", "PackRelease #1 cannot be published.", errors={"report": [f"{i['code']}: {i['message']}" for i in blockers]})
        pack_release = pack_releases.publish(user=user, note=note) if not blockers else pack_releases.current_release()
    return {
        "market_rate_set": {"uid": str(rate_set.uid), "name": rate_set.name, "outcome": rates_outcome},
        "price_release": price_release.number,
        "pack_release": pack_release.number,
        "published": not blockers,
        "report": pack_release.publish_report if not blockers else pack_preview,
        "violations": result.violations,
        "at": timezone.now().isoformat(),
    }
