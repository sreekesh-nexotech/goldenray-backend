"""PackReleases (PLAN §1.2, §2.5, §3.4): preview (the publish report), publish, read, compare.

A PackRelease prices every pack of the current approved configuration version against the current PriceRelease:

* BOM — ``engines.bom_builder.build_bom`` with the version's configuration and pins, component prices from the
  PriceRelease (``components.<sku>.list_price``);
* price — ``engines.pack_pricing.price_pack`` for the FLAT roof with no transport extra (the pack's base price; roof
  add-ons, swaps and transport are quotation-time extras), with the configuration's ``marketRates`` replaced by the
  PriceRelease's market rates (``market_rates_by_key``: the market-rate authority, PLAN §2.3);
* engineering — the checker at template scope with the ACTIVE rule set (``publishApprovedPacks`` semantics).

**Publish report** (``{can_publish, counts, items, matrix, summary, payload_sha256}``): ``items`` are
``{severity, code, message, context}``. BLOCK (publishing refused): ``NO_APPROVED_VERSION``, ``NO_PRICE_RELEASE``,
``NO_ACTIVE_RULE_SET``, ``NO_PUBLISHABLE_PACKS``, ``RELEASE_UNCHANGED``. A pack that cannot be sold is left out of the
release with a WARN naming why — Flarize publishes the other packs and reports the blocked combinations:
``MARKET_RATE_NOT_SET`` (no market rate in the PriceRelease), ``PACK_COMPONENT_NOT_SELECTABLE`` (a BOM component that
``catalog.services.assert_selectable`` refuses: retired or deleted), ``PACK_ENGINEERING_BLOCKED`` (PBC BLOCK findings
not waived by engineering), ``PACK_PRICING_BLOCKED``, ``PACK_BUILD_REFUSED``, ``PACK_ENGINEERING_UNAVAILABLE`` (the checker cannot run: no
architecture for the pack's profile). Also WARN ``PIN_COMPONENT_NOT_SELECTABLE``
and ``CONFIG_GST_DIFFERS``; INFO ``PACK_ENGINEERING_WAIVED`` (the pack is released: every BLOCK finding was
acknowledged) and ``CONFIG_MARKET_RATES_DIFFER``. ``matrix`` is the readiness matrix: one row per pack with its status
(READY / EXCLUDED), reasons and price.

**Publish** (``packs.publish``, one transaction): ``expected_current_number`` (409 ``stale_version``), no BLOCK item
(409 ``publish_blocked``), the checker run is stored (``engineering_run``), the previous release SUPERSEDED, the version
PUBLISHED, the next number from ``core.sequences`` (``PACK_RELEASE``), the payload + SHA-256 + report stored, cache
namespace ``packs`` bumped, ``packs.release_published`` emitted.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from decimal import ROUND_HALF_UP, Decimal

from django.db import IntegrityError, transaction
from django.utils import timezone

from audit.services import record
from catalog.models import Component
from catalog.services import ComponentNotSelectable, assert_selectable
from core.errors import Conflict, NotFound, StaleVersion
from core.outbox import emit
from core.sequences import ensure_next_value_at_least, next_value
from core.services import stamp_create
from engineering.models import Engine, SubjectType
from engineering.services import rule_sets, runs
from engines.engineering_checker import Severity
from flarize.cache_utils import bump
from packs.models import CURRENT_STATUSES, ConfigPin, ConfigStatus, ConfigVersion, PackRelease, ReleasePack, ReleaseStatus
from packs.services import engine
from packs.services.common import CACHE_NAMESPACE, json_safe, money, platform_system, platform_tier, sha256_of, size_kw
from packs.services.context import engine_context, version_pins
from packs.services.versions import current_version
from pricing.models import PriceRelease
from pricing.services.releases import current_release as current_price_release

PAYLOAD_SCHEMA = "flarize.pack-release/1"
BLOCK, WARN, INFO = "BLOCK", "WARN", "INFO"
SEQUENCE_KIND = "PACK_RELEASE"
FOUR = Decimal("0.0001")


@dataclass
class Report:
    items: list[dict] = field(default_factory=list)
    matrix: list[dict] = field(default_factory=list)

    def add(self, severity: str, code: str, message: str, **context) -> None:
        self.items.append({"severity": severity, "code": code, "message": message, "context": json_safe(context)})

    @property
    def blocked(self) -> bool:
        return any(item["severity"] == BLOCK for item in self.items)

    def as_dict(self, summary: dict, sha: str | None) -> dict:
        counts = {level: sum(1 for item in self.items if item["severity"] == level) for level in (BLOCK, WARN, INFO)}
        return {"can_publish": not self.blocked, "counts": counts, "items": self.items, "matrix": self.matrix, "summary": summary, "payload_sha256": sha}


@dataclass
class Build:
    report: Report
    version: ConfigVersion | None = None
    price_release: PriceRelease | None = None
    rule_set: object = None
    packs: list[dict] = field(default_factory=list)
    checks: list = field(default_factory=list)
    payload: dict = field(default_factory=dict)
    sha256: str | None = None


def current_release() -> PackRelease | None:
    return PackRelease.objects.filter(status=ReleaseStatus.PUBLISHED).select_related("config_version", "price_release", "published_by").first()


def releases_queryset():
    return PackRelease.objects.select_related("config_version", "price_release", "published_by").defer("payload", "publish_report").order_by("-number")


def get_release(number: int) -> PackRelease:
    release = PackRelease.objects.select_related("config_version", "price_release", "published_by").filter(number=number).first()
    if release is None:
        raise NotFound("release_not_found", f"There is no PackRelease #{number}.")
    return release


def release_packs(release: PackRelease):
    return ReleasePack.objects.filter(release=release).order_by("sort_order")


# ── building ───────────────────────────────────────────────────────────────────────────────────────────────────────


def _landed_total(bom: dict, pricing: dict, components: dict) -> Decimal:
    """The pack's reference cost (``pack_pricing`` ``internal.referenceCost``) with the material at landed cost where the
    PriceRelease has one (the catalog reference price otherwise)."""
    material = Decimal("0")
    for line in bom.get("lines") or []:
        qty = Decimal(str(line.get("qty") or 0))
        landed = (components.get(line.get("componentId")) or {}).get("landed_cost") if line.get("componentId") else None
        unit = Decimal(landed) if landed is not None else Decimal(str(line.get("unitPrice") or 0))
        material += qty * unit
    reference = (pricing.get("internal") or {}).get("referenceCost") or {}
    other = sum((Decimal(str(value)) for name, value in reference.items() if name != "material"), Decimal("0"))
    return money((material.quantize(Decimal("1"), rounding=ROUND_HALF_UP)) + other)


def _bom_lines(bom: dict, spec: engine.PackSpec, config: dict) -> list[dict]:
    lines = []
    for line in bom.get("lines") or []:
        lines.append(
            {
                "sku": line.get("componentId"),
                "name": line.get("name"),
                "category": line.get("category"),
                "qty": line.get("qty"),
                "unit_price": line.get("unitPrice"),
                "amount": line.get("amount"),
                "gst_pct": line.get("gst"),
                "gst_amount": line.get("gstAmt"),
                "source": engine_line_source(line),
            }
        )
    for item in engine.structure_lines(spec, config):
        lines.append(
            {
                "sku": None,
                "name": item.get("name"),
                "category": "structure",
                "qty": item.get("qty"),
                "unit_price": item.get("unitPrice"),
                "amount": item.get("amount"),
                "gst_pct": None,
                "gst_amount": None,
                "source": "STRUCTURE",
            }
        )
    return lines


def engine_line_source(line: dict) -> str:
    from packs.services.mirror import line_source

    return str(line_source(line))


def _catalog_skus(bom: dict) -> set[str]:
    """SKUs of the catalog components in a BOM (fixed items carry the configuration's own ids, not components)."""
    return {line["componentId"] for line in bom.get("lines") or [] if line.get("componentId") and line.get("category") != "fixed"}


def _gst_check(version: ConfigVersion, release: PriceRelease, report: Report) -> None:
    config_rate = ((version.config or {}).get("gst") or {}).get("ratePct")
    release_rate = ((release.payload or {}).get("gst") or {}).get("effective_rate_pct")
    if config_rate is None or release_rate is None:
        return
    if Decimal(str(config_rate)) != Decimal(str(release_rate)):
        report.add(WARN, "CONFIG_GST_DIFFERS", f"The configuration prices GST at {config_rate} %, PriceRelease #{release.number} at {release_rate} %.", config=config_rate, price_release=release_rate)


def _market_rate_diff(version: ConfigVersion, rates: dict, report: Report) -> None:
    own = (version.config or {}).get("marketRates") or {}
    differing = []
    for key, sizes in own.items():
        for size, value in (sizes or {}).items():
            other = (rates.get(key) or {}).get(size)
            if Decimal(str(value or 0)) != Decimal(str(other or 0)):
                differing.append(f"{key}/{size}")
    if differing:
        report.add(INFO, "CONFIG_MARKET_RATES_DIFFER", f"{len(differing)} market-rate cells of the configuration differ from the PriceRelease; the PriceRelease's are used.", cells=differing[:200])


def build(*, at: str | None = None) -> Build:
    report = Report()
    result = Build(report=report)
    version = current_version()
    price_release = current_price_release()
    if version is None:
        report.add(BLOCK, "NO_APPROVED_VERSION", "No pack configuration version is approved.")
    if price_release is None:
        report.add(BLOCK, "NO_PRICE_RELEASE", "No PriceRelease has been published.")
    try:
        rule_set = rule_sets.active_rule_set(Engine.CHECKER)
    except Conflict:
        rule_set = None
        report.add(BLOCK, "NO_ACTIVE_RULE_SET", "No engineering rule set is active.")
    if version is None or price_release is None or rule_set is None:
        return result
    result.version, result.price_release, result.rule_set = version, price_release, rule_set
    at = at or timezone.now().isoformat()
    ctx = engine_context(price_release)
    config = version.config
    rates = price_release.payload.get("market_rates_by_key") or {}
    priced_config = engine.pricing_config(config, rates)
    parsed_rules = rule_sets.engine_rule_set(rule_set)
    acknowledged = runs.acknowledged_identities(SubjectType.PACK_CONFIG_VERSION, version.uid)
    env = {"catalog": engine.checker_catalog(ctx.catalog, config), "batteryMaster": ctx.battery_master, "catalogVersion": price_release.number}
    released_components = price_release.payload.get("components") or {}
    pins = version_pins(version)
    _gst_check(version, price_release, report)
    _market_rate_diff(version, rates, report)
    for pin in ConfigPin.objects.filter(pack__config_version=version, pack__deleted_at__isnull=True).select_related("pack", "component"):
        try:
            assert_selectable(pin.component, field=f"{pin.pack.key}.{pin.slot_key}")
        except ComponentNotSelectable as exc:
            report.add(
                WARN,
                "PIN_COMPONENT_NOT_SELECTABLE",
                f"{pin.pack.key}: the pinned {pin.slot_key} {pin.component.sku} is not selectable ({exc.code}); the template decides instead.",
                pack=pin.pack.key,
                sku=pin.component.sku,
                reason=exc.code,
            )
    all_skus = set()
    boms = {}
    for spec in engine.enumerate_packs(config):
        try:
            boms[spec.key] = engine.build(spec, config=config, catalog=ctx.catalog, pins=pins.get(spec.key, []))
            all_skus.update(_catalog_skus(boms[spec.key]))
        except engine.EngineError as exc:
            boms[spec.key] = exc
    components: dict[str, Component] = {}
    for component in Component.all_objects.filter(sku__in=all_skus).order_by("id"):
        if component.sku not in components or components[component.sku].deleted_at is not None:
            components[component.sku] = component  # a live component wins over a deleted one with the same SKU
    for spec in engine.enumerate_packs(config):
        reasons = []
        row = {"key": spec.key, "display_name": engine.display_name(spec, config), "market_rate_key": spec.market_rate_key}
        bom = boms[spec.key]
        if isinstance(bom, Exception):
            reasons.append("PACK_BUILD_REFUSED")
            report.add(WARN, "PACK_BUILD_REFUSED", f"{spec.key}: the BOM builder refused the pack: {getattr(bom, 'message', bom)}", pack=spec.key, engine_code=getattr(bom, "code", None))
            report.matrix.append({**row, "status": "EXCLUDED", "reasons": reasons, "price": None})
            continue
        for sku in sorted(_catalog_skus(bom)):
            try:
                assert_selectable(components.get(sku), field=sku)
            except ComponentNotSelectable as exc:
                if "PACK_COMPONENT_NOT_SELECTABLE" not in reasons:
                    reasons.append("PACK_COMPONENT_NOT_SELECTABLE")
                report.add(WARN, "PACK_COMPONENT_NOT_SELECTABLE", f"{spec.key}: {sku} cannot be sold ({exc.code}).", pack=spec.key, sku=sku, reason=exc.code)
        try:
            check = engine.check(spec, bom, checker_env=env, rule_set=parsed_rules, at=at)
        except engine.EngineError as exc:
            reasons.append("PACK_ENGINEERING_UNAVAILABLE")
            report.add(WARN, "PACK_ENGINEERING_UNAVAILABLE", f"{spec.key}: the engineering checker cannot run: {getattr(exc, 'message', exc)}", pack=spec.key, engine_code=getattr(exc, "code", None))
            report.matrix.append({**row, "status": "EXCLUDED", "reasons": reasons, "price": None})
            continue
        result.checks.append((spec.key, check))
        blockers = [f for f in check.findings if f.severity == Severity.BLOCK]
        open_blockers = [f for f in blockers if runs.identity(spec.key, f.rule_id, f.component_ids) not in acknowledged]
        if open_blockers:
            reasons.append("PACK_ENGINEERING_BLOCKED")
            report.add(
                WARN,
                "PACK_ENGINEERING_BLOCKED",
                f"{spec.key}: blocked by the engineering checker ({', '.join(sorted({f.rule_id for f in open_blockers}))}).",
                pack=spec.key,
                findings=[{"rule_code": f.rule_id, "component_ids": list(f.component_ids), "message": f.message} for f in open_blockers],
            )
        elif blockers:
            report.add(INFO, "PACK_ENGINEERING_WAIVED", f"{spec.key}: BLOCK findings waived by engineering ({', '.join(sorted({f.rule_id for f in blockers}))}).", pack=spec.key)
        pricing = engine.price(spec, bom, config=priced_config, at=at)
        if pricing.get("status") != "COMPLETE":
            codes = [error.get("code") for error in pricing.get("errors") or []]
            code = "MARKET_RATE_NOT_SET" if "MARKET_RATE_NOT_SET" in codes else "PACK_PRICING_BLOCKED"
            reasons.append(code)
            report.add(WARN, code, f"{spec.key}: {'; '.join(error.get('message', '') for error in pricing.get('errors') or [])}", pack=spec.key, key=spec.market_rate_key, size=spec.size, codes=codes)
        price = pricing.get("customer", {}).get("sellingPriceIncludingGST") if pricing.get("status") == "COMPLETE" else None
        report.matrix.append({**row, "status": "EXCLUDED" if reasons else "READY", "reasons": reasons, "price": price, "engineering": check.status.value})
        if reasons:
            continue
        customer = pricing["customer"]
        incl, excl, gst = money(customer["sellingPriceIncludingGST"]), money(customer["sellingPriceBeforeGST"]), money(customer["gstAmount"])
        landed = _landed_total(bom, pricing, released_components)
        margin = ((excl - landed) / excl).quantize(FOUR, rounding=ROUND_HALF_UP) if excl else Decimal("0")
        result.packs.append(
            {
                "key": spec.key,
                "system_type": platform_system(spec.system),
                "tier": platform_tier(spec.tier),
                "size_key": spec.size,
                "size_kw": size_kw(spec.size),
                "phase": spec.phase,
                "battery_config": spec.battery_config,
                "future_size_key": spec.future,
                "future_size_kw": size_kw(spec.future) if spec.future else None,
                "market_rate_key": spec.market_rate_key,
                "customer_price_incl_gst": incl,
                "customer_price_excl_gst": excl,
                "gst_amount": gst,
                "landed_cost_total": landed,
                "gross_margin_pct": margin,
                "bom": _bom_lines(bom, spec, config),
                "pricing": pricing,
                "display_name": engine.display_name(spec, config),
                "profile_key": str((bom.get("systemConfig") or {}).get("profileKey") or ""),
                "engineering_result": check.result.value,
                "sort_order": spec.sort_order,
            }
        )
    if not result.packs:
        report.add(BLOCK, "NO_PUBLISHABLE_PACKS", "No pack of the approved configuration can be sold; see the matrix.")
    result.payload = json_safe(
        {
            "schema": PAYLOAD_SCHEMA,
            "config_version": {"uid": str(version.uid), "number": version.number},
            "price_release": {"uid": str(price_release.uid), "number": price_release.number, "payload_sha256": price_release.payload_sha256},
            "rules_version": rule_set.rules_version,
            "packs": {pack["key"]: {name: value for name, value in pack.items() if name not in ("pricing",)} for pack in result.packs},
        }
    )
    result.sha256 = sha256_of(result.payload)
    current = current_release()
    if current is not None and current.payload_sha256 == result.sha256:
        report.add(BLOCK, "RELEASE_UNCHANGED", f"Nothing changed since PackRelease #{current.number}.", current=current.number)
    return result


def summary_of(built: Build) -> dict:
    return {
        "config_version": built.version.number if built.version else None,
        "price_release": built.price_release.number if built.price_release else None,
        "rules_version": getattr(built.rule_set, "rules_version", None),
        "packs_total": len(built.report.matrix),
        "packs_ready": len(built.packs),
        "packs_excluded": sum(1 for row in built.report.matrix if row["status"] == "EXCLUDED"),
    }


def preview() -> dict:
    built = build()
    report = built.report.as_dict(summary_of(built), built.sha256)
    current = current_release()
    report["current_number"] = current.number if current else None
    report["next_number"] = (current.number + 1) if current else 1
    return report


@transaction.atomic
def publish(*, user, note: str = "", expected_current_number=None) -> PackRelease:
    current = PackRelease.objects.select_for_update().filter(status=ReleaseStatus.PUBLISHED).first()
    if expected_current_number is not None and (current.number if current else 0) != expected_current_number:
        raise StaleVersion("stale_version", "Another PackRelease was published meanwhile; review the new preview.", errors={"expected_current_number": [str(current.number if current else 0)]})
    built = build()
    report = built.report.as_dict(summary_of(built), built.sha256)
    if built.report.blocked:
        raise Conflict(
            "publish_blocked", "The publish report has blocking items.", errors={"report": [f"{item['code']}: {item['message']}" for item in built.report.items if item["severity"] == BLOCK]}
        )
    version = ConfigVersion.objects.select_for_update().get(pk=built.version.pk)
    if version.status not in CURRENT_STATUSES or version.config != built.version.config:
        # another version was approved between the (unlocked) build and this lock: the report no longer describes it
        raise Conflict("publish_conflict", f"v{version.number} is no longer the approved configuration; review the new preview.")
    run = runs.record_run(
        rule_set=built.rule_set,
        subject_type=SubjectType.PACK_CONFIG_VERSION,
        subject_uid=version.uid,
        subject_label=f"Pack config v{version.number} (publish)",
        checks=built.checks,
        user=user,
    )
    now = timezone.now()
    if current is not None:
        current.versioned_update(user, status=ReleaseStatus.SUPERSEDED, superseded_at=now)
        ensure_next_value_at_least(SEQUENCE_KIND, current.number + 1)
    number = next_value(SEQUENCE_KIND)
    release = PackRelease(
        number=number,
        config_version=version,
        price_release=built.price_release,
        status=ReleaseStatus.PUBLISHED,
        published_at=now,
        published_by=user if getattr(user, "pk", None) else None,
        note=note or "",
        publish_report={**report, "engineering_run": str(run.uid)},
        payload=built.payload,
        payload_sha256=built.sha256,
    )
    stamp_create(release, user)
    try:
        with transaction.atomic():
            release.save()
    except IntegrityError:
        raise Conflict("publish_conflict", "Another PackRelease was published at the same time; reload.") from None
    ReleasePack.objects.bulk_create([ReleasePack(release=release, **{name: json_safe(value) if name in ("bom", "pricing") else value for name, value in pack.items()}) for pack in built.packs])
    if version.status == ConfigStatus.APPROVED:
        version.versioned_update(user, status=ConfigStatus.PUBLISHED)
    record(
        "packs.release_published",
        obj=release,
        actor=user,
        after={
            "number": number,
            "config_version": version.number,
            "price_release": built.price_release.number,
            "packs": len(built.packs),
            "payload_sha256": built.sha256,
            "superseded": current.number if current else None,
        },
        note=note,
    )
    bump(CACHE_NAMESPACE)
    emit(
        "packs.release_published",
        {
            "release_uid": str(release.uid),
            "number": number,
            "previous_number": current.number if current else None,
            "config_version_number": version.number,
            "price_release_number": built.price_release.number,
            "packs": len(built.packs),
            "payload_sha256": built.sha256,
        },
        aggregate_type="packs.release",
        aggregate_uid=release.uid,
        dedup_key=f"packs.release_published:{number}",
    )
    return release


# ── compare ────────────────────────────────────────────────────────────────────────────────────────────────────────


def compare(a: int, b: int) -> dict:
    """Pack by pack: added / removed keys and, for packs in both, price and BOM line differences (by SKU or name)."""
    left, right = get_release(a), get_release(b)
    old = {pack.key: pack for pack in release_packs(left)}
    new = {pack.key: pack for pack in release_packs(right)}
    changed = []
    for key in sorted(set(old) & set(new)):
        before, after = old[key], new[key]
        entry = {"key": key, "display_name": after.display_name, "price_before": before.customer_price_incl_gst, "price_after": after.customer_price_incl_gst, "lines": []}

        def index(bom):
            return {(line.get("category"), line.get("sku") or line.get("name")): line for line in bom or []}

        lines_before, lines_after = index(before.bom), index(after.bom)
        for line_key in sorted(set(lines_before) | set(lines_after), key=str):
            x, y = lines_before.get(line_key), lines_after.get(line_key)
            if x is None or y is None or x.get("qty") != y.get("qty") or x.get("unit_price") != y.get("unit_price"):
                entry["lines"].append(
                    {
                        "category": line_key[0],
                        "item": line_key[1],
                        "qty_before": x.get("qty") if x else None,
                        "qty_after": y.get("qty") if y else None,
                        "unit_price_before": x.get("unit_price") if x else None,
                        "unit_price_after": y.get("unit_price") if y else None,
                    }
                )
        if entry["price_before"] != entry["price_after"] or entry["lines"]:
            changed.append(entry)
    return {"a": left.number, "b": right.number, "added": sorted(set(new) - set(old)), "removed": sorted(set(old) - set(new)), "changed": changed}
