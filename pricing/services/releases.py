"""PriceReleases (PLAN §1.2, §2.3): preview (the publish report), publish, detail, diff.

``preview()`` builds the release exactly as ``publish()`` would — without writing — and returns the **publish report**:
every data blocker and warning, each ``{code, severity (BLOCK|WARN|INFO), message, context}``. ``publish()`` refuses
(409 ``publish_blocked``, the report in ``errors``) while any BLOCK item exists; otherwise, in one transaction, it
writes the release (next number), its lines, the payload and its sha256, marks the previous release SUPERSEDED, bumps
the ``pricing`` cache namespace (the website's product prices) and emits ``pricing.release_published``.

BLOCK: no ACTIVE market rate set; GST configuration missing or inconsistent; nothing changed since the current
release (prices can never be negative: a database check). WARN: an ACTIVE/DEPRECATED component without a LIST price or with a zero LIST price, an ACTIVE
component without a landed cost, market-rate cells not set (0), swap deltas naming retired components, website-BOM
cost keys missing, no installation matrix, no validity policy. INFO: retired components' prices left out.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from decimal import Decimal

from django.db import IntegrityError, transaction
from django.utils import timezone

from audit.services import record
from catalog.models import Component, ComponentStatus
from core.errors import Conflict, NotFound, StaleVersion
from core.outbox import emit
from core.sequences import ensure_next_value_at_least, next_value
from core.services import stamp_create
from engines import money
from flarize.cache_utils import bump
from pricing.models import CurrentPrice, MarketRateSet, PriceKind, PriceRelease, PriceReleaseLine, ReleaseStatus
from pricing.services import cost_config, market_rates, masters, validity
from pricing.services.common import CACHE_NAMESPACE, decimal_text, json_safe, money_text, sha256_of, today

PAYLOAD_SCHEMA = "flarize.price-release/1"
BLOCK, WARN, INFO = "BLOCK", "WARN", "INFO"
SEQUENCE_KIND = "PRICE_RELEASE"
SELLABLE = (ComponentStatus.ACTIVE, ComponentStatus.DEPRECATED)
INTERNAL_COMPONENT_FIELDS = ("landed_cost", "landed_effective_from", "landed_version_key")
WEBSITE_BOM_KEYS = tuple(spec.key for spec in cost_config.KEYS.values() if spec.group == "website_bom")


@dataclass
class Report:
    items: list[dict] = field(default_factory=list)

    def add(self, severity: str, code: str, message: str, **context) -> None:
        self.items.append({"severity": severity, "code": code, "message": message, "context": json_safe(context)})

    @property
    def blocked(self) -> bool:
        return any(item["severity"] == BLOCK for item in self.items)

    def as_dict(self, summary: dict, payload_sha256: str | None) -> dict:
        counts = {level: sum(1 for item in self.items if item["severity"] == level) for level in (BLOCK, WARN, INFO)}
        return {"can_publish": not self.blocked, "counts": counts, "items": self.items, "summary": summary, "payload_sha256": payload_sha256}


@dataclass
class Build:
    payload: dict
    lines: list[dict]
    report: Report
    market_rate_set: MarketRateSet | None
    sha256: str


def current_release() -> PriceRelease | None:
    return PriceRelease.objects.filter(status=ReleaseStatus.PUBLISHED).select_related("market_rate_set", "published_by").first()


def releases_queryset():
    return PriceRelease.objects.select_related("market_rate_set", "published_by").defer("payload").order_by("-number")


def get_release(number: int) -> PriceRelease:
    release = PriceRelease.objects.select_related("market_rate_set", "published_by").filter(number=number).first()
    if release is None:
        raise NotFound("release_not_found", f"There is no PriceRelease #{number}.")
    return release


# ── building ───────────────────────────────────────────────────────────────────────────────────────────────────────


def _components(report: Report) -> tuple[dict, list[dict]]:
    prices: dict[int, dict[str, CurrentPrice]] = {}
    for row in CurrentPrice.objects.filter(kind__in=[PriceKind.LIST, PriceKind.LANDED]).order_by("component_id", "kind"):
        prices.setdefault(row.component_id, {})[row.kind] = row
    components = Component.objects.filter(pk__in=list(prices)).select_related("category").order_by("sku")
    sellable = Component.objects.filter(status__in=SELLABLE).select_related("category").order_by("sku")
    payload, lines, retired = {}, [], []
    for component in components:
        if component.status == ComponentStatus.RETIRED:
            retired.append(component.sku)
            continue
        found = prices[component.pk]
        listed, landed = found.get(PriceKind.LIST), found.get(PriceKind.LANDED)
        gst_rate = component.effective_gst_rate
        payload[component.sku] = {
            "uid": str(component.uid),
            "name": component.name,
            "category": component.category.slug,
            "status": component.status,
            "gst_rate": decimal_text(gst_rate),
            "list_price": money_text(listed.amount) if listed else None,
            "list_price_gst_inclusive": listed.gst_inclusive if listed else False,
            "per_watt": decimal_text(listed.per_watt) if listed and listed.per_watt is not None else None,
            "list_effective_from": listed.effective_from.isoformat() if listed else None,
            "landed_cost": money_text(landed.amount) if landed else None,
            "landed_effective_from": landed.effective_from.isoformat() if landed else None,
            "landed_version_key": landed.version_key if landed else None,
        }
        lines.append(
            {
                "component_id": component.pk,
                "list_price": listed.amount if listed else None,
                "list_price_gst_inclusive": listed.gst_inclusive if listed else False,
                "landed_cost": landed.amount if landed else None,
                "gst_rate": gst_rate,
            }
        )
    if retired:
        report.add(INFO, "RETIRED_COMPONENTS_EXCLUDED", f"{len(retired)} retired components with current prices are left out.", skus=retired)
    no_list = [c.sku for c in sellable if PriceKind.LIST not in prices.get(c.pk, {})]
    zero_list = [c.sku for c in sellable if PriceKind.LIST in prices.get(c.pk, {}) and prices[c.pk][PriceKind.LIST].amount == 0]
    no_landed = [c.sku for c in sellable if c.status == ComponentStatus.ACTIVE and PriceKind.LANDED not in prices.get(c.pk, {})]
    if no_list:
        report.add(WARN, "LIST_PRICE_MISSING", f"{len(no_list)} active/deprecated components have no LIST price.", skus=no_list)
    if zero_list:
        report.add(WARN, "LIST_PRICE_ZERO", f"{len(zero_list)} active/deprecated components have a zero LIST price.", skus=zero_list)
    if no_landed:
        report.add(WARN, "LANDED_COST_MISSING", f"{len(no_landed)} active components have no landed cost.", skus=no_landed)
    return payload, lines


def _market_rates(rate_set: MarketRateSet | None, report: Report) -> dict:
    if rate_set is None:
        report.add(BLOCK, "MARKET_RATE_SET_MISSING", "No market rate set is ACTIVE; activate one first.")
        return {"market_rate_set": None, "market_rates": [], "market_rates_by_key": {}, "swap_deltas": [], "roof_addons": []}
    rates = list(market_rates.rates_of(rate_set))
    by_key: dict[str, dict] = {}
    unset: dict[str, list[str]] = {}
    for rate in rates:
        key = market_rates.rate_key(rate)
        by_key.setdefault(key, {})[rate.size_key] = decimal_text(rate.customer_price_incl_gst) if rate.customer_price_incl_gst else "0"
        if not rate.customer_price_incl_gst:
            unset.setdefault(key, []).append(rate.size_key)
    for key, sizes in unset.items():
        report.add(WARN, "MARKET_RATE_NOT_SET", f"Market rate not set for {key}: {', '.join(sizes)}.", key=key, sizes=sizes)
    deltas = []
    for delta in market_rates.swap_deltas_of(rate_set):
        dead = [c.sku for c in (delta.from_component, delta.to_component) if c.deleted_at is not None or c.status == ComponentStatus.RETIRED]
        if dead:
            report.add(WARN, "SWAP_DELTA_RETIRED_COMPONENT", f"A {delta.slot} swap names retired/deleted components: {', '.join(dead)}.", skus=dead)
        deltas.append(
            {
                "system_type": delta.system_type,
                "tier": delta.tier,
                "slot": delta.slot,
                "from_sku": delta.from_component.sku,
                "to_sku": delta.to_component.sku,
                "delta_incl_gst": money_text(delta.delta_incl_gst),
            }
        )
    addons = [{"structure_type": addon.structure_type, "size_kw": decimal_text(addon.size_kw), "addon_incl_gst": money_text(addon.addon_incl_gst)} for addon in market_rates.roof_addons_of(rate_set)]
    return {
        "market_rate_set": {"uid": str(rate_set.uid), "name": rate_set.name, "activated_at": rate_set.activated_at.isoformat() if rate_set.activated_at else None},
        "market_rates": [market_rates.rate_summary(rate) for rate in rates],
        "market_rates_by_key": by_key,
        "swap_deltas": deltas,
        "roof_addons": addons,
    }


def _gst(values: dict, report: Report) -> dict | None:
    missing = [key for key in cost_config.GST_KEYS if key not in values]
    if missing:
        report.add(BLOCK, "GST_CONFIG_MISSING", f"GST configuration keys missing: {', '.join(missing)}.", keys=missing)
        return None
    try:
        regime = money.resolve_gst_regime(money.GstConfig.from_cost_config(**{key: values[key] for key in cost_config.GST_KEYS}))
    except (money.GstConfigError, ValueError, TypeError) as exc:
        report.add(BLOCK, "GST_CONFIG_INVALID", f"The GST split is inconsistent: {getattr(exc, 'reason', exc)}", engine_code=str(getattr(exc, "code", "")))
        return None
    return {
        "regime": str(regime.regime),
        "effective_rate_pct": money.canonical(regime.effective_rate_pct),
        **{key: values[key] for key in cost_config.GST_KEYS},
    }


def build() -> Build:
    report = Report()
    rate_set = market_rates.active_set()
    components, lines = _components(report)
    rates = _market_rates(rate_set, report)
    config = cost_config.current_values()
    missing = [key for key in WEBSITE_BOM_KEYS if key not in config]
    if missing:
        report.add(WARN, "COST_CONFIG_MISSING", f"Cost configuration keys not set: {', '.join(missing)}.", keys=missing)
    matrix = [
        {
            "size_kw": decimal_text(row.size_kw),
            "phase": row.phase,
            "installation_type": row.installation_type,
            "install_cost": money_text(row.install_cost),
            "labour_days": decimal_text(row.labour_days),
        }
        for row in masters.matrix_queryset()
    ]
    if not matrix:
        report.add(WARN, "INSTALLATION_MATRIX_EMPTY", "The installation matrix is empty.")
    on = today()
    fees = [
        {
            "kind": fee.kind,
            "label": fee.label,
            "phase": fee.phase,
            "capacity_kw_max": decimal_text(fee.capacity_kw_max),
            "amount": money_text(fee.amount),
            "effective_from": fee.effective_from.isoformat(),
        }
        for fee in masters.fees_queryset().filter(effective_from__lte=on)
        if fee.effective_to is None or fee.effective_to >= on
    ]
    policy = validity.policy_payload()
    if policy is None:
        report.add(WARN, "VALIDITY_POLICY_MISSING", "No quotation validity policy is configured.")
    payload = {
        "schema": PAYLOAD_SCHEMA,
        "currency": "INR",
        **rates,
        "components": components,
        "cost_config": config,
        "installation_matrix": matrix,
        "statutory_fees": fees,
        "validity_policy": policy,
        "gst": _gst(config, report),
    }
    payload = json_safe(payload)
    sha = sha256_of(payload)
    current = current_release()
    if current is not None and current.payload_sha256 == sha:
        report.add(BLOCK, "RELEASE_UNCHANGED", f"Nothing changed since PriceRelease #{current.number}.", current=current.number)
    return Build(payload=payload, lines=lines, report=report, market_rate_set=rate_set, sha256=sha)


def summary_of(payload: dict) -> dict:
    components = payload.get("components") or {}
    return {
        "components": len(components),
        "list_prices": sum(1 for item in components.values() if item.get("list_price") is not None),
        "landed_costs": sum(1 for item in components.values() if item.get("landed_cost") is not None),
        "market_rates": len(payload.get("market_rates") or []),
        "swap_deltas": len(payload.get("swap_deltas") or []),
        "roof_addons": len(payload.get("roof_addons") or []),
        "cost_config_keys": len(payload.get("cost_config") or {}),
        "installation_matrix": len(payload.get("installation_matrix") or []),
        "statutory_fees": len(payload.get("statutory_fees") or []),
    }


def preview() -> dict:
    built = build()
    current = current_release()
    report = built.report.as_dict(summary_of(built.payload), built.sha256)
    report["next_number"] = (current.number + 1) if current else 1
    report["current_number"] = current.number if current else None
    report["changes"] = diff_payloads(current.payload if current else {}, built.payload, counts_only=True)
    return report


@transaction.atomic
def publish(*, user, note: str = "", expected_current_number=None) -> PriceRelease:
    current = PriceRelease.objects.select_for_update().filter(status=ReleaseStatus.PUBLISHED).first()
    if expected_current_number is not None and (current.number if current else 0) != expected_current_number:
        raise StaleVersion("stale_version", "Another release was published meanwhile; review the new preview.", errors={"expected_current_number": [str(current.number if current else 0)]})
    built = build()
    report = built.report.as_dict(summary_of(built.payload), built.sha256)
    if built.report.blocked:
        raise Conflict(
            "publish_blocked", "The publish report has blocking items.", errors={"report": [f"{item['code']}: {item['message']}" for item in built.report.items if item["severity"] == BLOCK]}
        )
    now = timezone.now()
    if current is not None:
        current.versioned_update(user, status=ReleaseStatus.SUPERSEDED, superseded_at=now)
        ensure_next_value_at_least(SEQUENCE_KIND, current.number + 1)
    number = next_value(SEQUENCE_KIND)
    release = PriceRelease(
        number=number,
        status=ReleaseStatus.PUBLISHED,
        published_at=now,
        published_by=user if getattr(user, "pk", None) else None,
        market_rate_set=built.market_rate_set,
        note=note or "",
        payload=built.payload,
        payload_sha256=built.sha256,
        publish_report=report,
    )
    stamp_create(release, user)
    try:
        with transaction.atomic():
            release.save()
    except IntegrityError:
        raise Conflict("publish_conflict", "Another release was published at the same time; reload.") from None
    PriceReleaseLine.objects.bulk_create([PriceReleaseLine(release=release, **line) for line in built.lines])
    record(
        "pricing.release_published",
        obj=release,
        actor=user,
        after={"number": number, "payload_sha256": built.sha256, "summary": report["summary"], "warnings": report["counts"][WARN], "superseded": current.number if current else None},
        note=note,
    )
    bump(CACHE_NAMESPACE)
    emit(
        "pricing.release_published",
        {
            "release_uid": str(release.uid),
            "number": number,
            "previous_number": current.number if current else None,
            "market_rate_set_uid": str(built.market_rate_set.uid),
            "payload_sha256": built.sha256,
        },
        aggregate_type="pricing.release",
        aggregate_uid=release.uid,
        dedup_key=f"pricing.release_published:{number}",
    )
    return release


# ── reading ────────────────────────────────────────────────────────────────────────────────────────────────────────


def redact_payload(payload: dict, *, internal: bool) -> dict:
    """The payload a caller may see: landed costs and margin configuration need ``pricing_internal.view``."""
    if internal or not payload:
        return payload
    redacted = dict(payload)
    redacted["components"] = {sku: {name: value for name, value in item.items() if name not in INTERNAL_COMPONENT_FIELDS} for sku, item in (payload.get("components") or {}).items()}
    config = {}
    for key, value in (payload.get("cost_config") or {}).items():
        shown = cost_config.redact(key, value, internal=False)
        if shown is not None:
            config[key] = shown
    redacted["cost_config"] = config
    return redacted


def _sections(payload: dict) -> dict[str, dict]:
    """Every comparable section of a payload as ``{natural key: record}``."""

    def keyed(rows, key_of):
        return {key_of(row): row for row in rows or []}

    return {
        "components": dict(payload.get("components") or {}),
        "market_rates": keyed(
            payload.get("market_rates"), lambda r: f"{r['key']}/{r['size_key']}" + (f"/from{r['from_size_key']}" if r.get("from_size_key") and not r["key"].startswith("upgrade_") else "")
        ),
        "swap_deltas": keyed(payload.get("swap_deltas"), lambda r: f"{r['system_type']}/{r['tier']}/{r['slot']}/{r['from_sku']}->{r['to_sku']}"),
        "roof_addons": keyed(payload.get("roof_addons"), lambda r: f"{r['structure_type']}/{r['size_kw']}"),
        "cost_config": {key: {"value": value} for key, value in (payload.get("cost_config") or {}).items()},
        "installation_matrix": keyed(payload.get("installation_matrix"), lambda r: f"{r['size_kw']}/{r['phase'] or 'any'}/{r['installation_type']}"),
        "statutory_fees": keyed(payload.get("statutory_fees"), lambda r: f"{r['kind']}/{r['phase'] or 'any'}/{r['capacity_kw_max'] or 'any'}"),
        "gst": {"gst": payload.get("gst") or {}},
        "validity_policy": {"validity": payload.get("validity_policy") or {}},
    }


def diff_payloads(old: dict, new: dict, *, counts_only: bool = False) -> dict:
    """Section by section: ``added``/``removed`` keys and ``changed`` fields (``{key, field, old, new}``)."""
    result = {}
    old_sections, new_sections = _sections(old or {}), _sections(new or {})
    for name, new_rows in new_sections.items():
        old_rows = old_sections.get(name, {})
        added = sorted(set(new_rows) - set(old_rows))
        removed = sorted(set(old_rows) - set(new_rows))
        changed = []
        for key in sorted(set(new_rows) & set(old_rows)):
            before, after = old_rows[key], new_rows[key]
            if before == after:
                continue
            if isinstance(before, dict) and isinstance(after, dict):
                for field_name in sorted(set(before) | set(after)):
                    if before.get(field_name) != after.get(field_name):
                        changed.append({"key": key, "field": field_name, "old": before.get(field_name), "new": after.get(field_name)})
            else:
                changed.append({"key": key, "field": "", "old": before, "new": after})
        if counts_only:
            result[name] = {"added": len(added), "removed": len(removed), "changed": len(changed)}
        else:
            result[name] = {"added": added, "removed": removed, "changed": changed}
    return result


def diff(*, against: int | None = None, internal: bool = False) -> dict:
    current = current_release()
    if current is None:
        raise NotFound("no_current_release", "No PriceRelease has been published yet.")
    if against is None:
        base = PriceRelease.objects.filter(number__lt=current.number).order_by("-number").first()
    else:
        base = get_release(against)
    base_payload = redact_payload(base.payload, internal=internal) if base else {}
    current_payload = redact_payload(current.payload, internal=internal)
    return {"current": current.number, "against": base.number if base else None, "sections": diff_payloads(base_payload, current_payload)}


def release_lines(release: PriceRelease):
    return PriceReleaseLine.objects.filter(release=release).select_related("component")


def gst_inclusive_list_price(line: PriceReleaseLine) -> Decimal | None:
    """The customer-facing list price of a release line: GST added unless already included; whole rupees."""
    if line.list_price is None or line.list_price <= 0:
        return None
    if line.list_price_gst_inclusive:
        return line.list_price
    return money.round_money(line.list_price * (1 + line.gst_rate))
