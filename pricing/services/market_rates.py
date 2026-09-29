"""``pricing/market-rate-sets/`` — sets, their rates / swap deltas / roof add-ons, activation (PLAN §2.3, §3.4).

* A set is created DRAFT (optionally copying every child row of another set); only DRAFT sets are edited, their
  children replaced with bulk PUTs (upsert by natural key, rows left out are soft-deleted). Every child write bumps
  the set's ``version``, so a client holding an old version gets 409 ``stale_version``.
* ``activate/`` makes a DRAFT (or a RETIRED, for a rollback) set ACTIVE and retires the previous ACTIVE one in the
  same transaction — exactly one ACTIVE set exists (partial unique index). The next PriceRelease copies it.
"""

from __future__ import annotations

import re
from decimal import Decimal

from django.db import IntegrityError, transaction
from django.db.models import Count, Q
from django.utils import timezone

from audit.services import changes, record, snapshot
from core.errors import Conflict, DomainError
from core.outbox import emit
from core.services import check_version, stamp_create
from flarize.cache_utils import bump
from pricing.models import MarketRate, MarketRateSet, MarketRateSetStatus, PriceRelease, RoofAddon, SwapDelta, SystemType
from pricing.services.common import AUTHORING_NAMESPACE, MAX_SIZE_KW, decimal_text, valid_size_key

SET_FIELDS = ("name", "status", "note", "activated_at", "retired_at")
SIZE_KEY_HELP = f"A size key like 3, 5sp, 5tp or 10 (more than 0 and at most {MAX_SIZE_KW} kW, 2 decimals)."
VARIANT_RE = re.compile(r"^[A-Za-z0-9]{1,24}$")
EDITABLE = (MarketRateSetStatus.DRAFT,)


def sets_queryset():
    return MarketRateSet.objects.annotate(
        rate_count=Count("rates", filter=Q(rates__deleted_at__isnull=True), distinct=True),
        swap_delta_count=Count("swap_deltas", filter=Q(swap_deltas__deleted_at__isnull=True), distinct=True),
        roof_addon_count=Count("roof_addons", filter=Q(roof_addons__deleted_at__isnull=True), distinct=True),
    ).order_by("-created_at", "-id")


def active_set() -> MarketRateSet | None:
    return MarketRateSet.objects.filter(status=MarketRateSetStatus.ACTIVE).first()


def rates_of(rate_set: MarketRateSet):
    return MarketRate.objects.filter(set=rate_set).order_by("system_type", "tier", "battery_config", "from_size_key", "future_size_key", "variant", "sort_order", "id")


def swap_deltas_of(rate_set: MarketRateSet):
    return SwapDelta.objects.filter(set=rate_set).select_related("from_component", "to_component").order_by("system_type", "tier", "slot", "id")


def roof_addons_of(rate_set: MarketRateSet):
    return RoofAddon.objects.filter(set=rate_set).order_by("structure_type", "size_kw", "id")


def rate_key(rate: MarketRate) -> str:
    """The Flarize market-rate key of a row (``ongrid_value``, ``hybrid_base_1_up10``, ``upgrade_3_5``)."""
    if rate.system_type == SystemType.UPGRADE:
        return f"upgrade_{rate.from_size_key}_{rate.size_key}"
    key = f"{rate.system_type.lower()}_{(rate.tier or '').lower()}"
    if rate.system_type == SystemType.HYBRID:
        key += f"_{rate.battery_config or '0'}"
    if rate.future_size_key:
        key += f"_up{rate.future_size_key}"
    if rate.variant:
        key += f"_{rate.variant}"
    return key


def natural_key(values: dict) -> tuple:
    return (
        values["system_type"],
        values.get("tier", ""),
        values.get("battery_config", ""),
        values["size_key"],
        values.get("from_size_key", ""),
        values.get("future_size_key", ""),
        values.get("variant", ""),
    )


def _locked_set(rate_set: MarketRateSet, expected_version=None, *, editable: bool = True) -> MarketRateSet:
    locked = MarketRateSet.objects.select_for_update().get(pk=rate_set.pk)
    check_version(locked, expected_version)
    if editable and locked.status not in EDITABLE:
        raise Conflict("market_rate_set_not_draft", f"Only DRAFT sets are edited; this set is {locked.status}. Create a new set (copy_from) instead.", errors={"status": [locked.status]})
    return locked


# ── sets ───────────────────────────────────────────────────────────────────────────────────────────────────────────


def _name_conflict() -> Conflict:
    return Conflict("market_rate_set_name_taken", "Another set already has this name.", errors={"name": ["Already in use."]})


@transaction.atomic
def create_set(*, user, data: dict) -> MarketRateSet:
    source = data.get("copy_from")
    rate_set = MarketRateSet(name=(data.get("name") or "").strip(), note=data.get("note", ""))
    if not rate_set.name:
        raise DomainError("validation_error", "A set needs a name.", errors={"name": ["This field may not be blank."]})
    stamp_create(rate_set, user)
    try:
        with transaction.atomic():
            rate_set.save()
    except IntegrityError:
        raise _name_conflict() from None
    copied = _copy_children(source, rate_set, user=user) if source is not None else 0
    record("pricing.market_rate_set_created", obj=rate_set, actor=user, after={**snapshot(rate_set, SET_FIELDS), "copied_from": str(source.uid) if source else None, "copied_rows": copied})
    bump(AUTHORING_NAMESPACE)
    return rate_set


def _copy_children(source: MarketRateSet, target: MarketRateSet, *, user) -> int:
    count = 0
    for model, fields in (
        (MarketRate, ("system_type", "tier", "battery_config", "size_kw", "size_key", "phase", "from_size_key", "from_size_kw", "future_size_key", "variant", "customer_price_incl_gst", "sort_order")),
        (SwapDelta, ("system_type", "tier", "slot", "from_component_id", "to_component_id", "delta_incl_gst")),
        (RoofAddon, ("structure_type", "size_kw", "addon_incl_gst")),
    ):
        rows = []
        for row in model.objects.filter(set=source).order_by("id"):
            copy = model(set=target, **{name: getattr(row, name) for name in fields})
            stamp_create(copy, user)
            rows.append(copy)
        model.objects.bulk_create(rows)
        count += len(rows)
    return count


@transaction.atomic
def update_set(instance: MarketRateSet, *, user, data: dict, expected_version=None) -> MarketRateSet:
    rate_set = _locked_set(instance, expected_version, editable=False)
    values = {name: data[name] for name in ("name", "note") if name in data}
    if "name" in values:
        values["name"] = (values["name"] or "").strip()
        if not values["name"]:
            raise DomainError("validation_error", "A set needs a name.", errors={"name": ["This field may not be blank."]})
    values = {name: value for name, value in values.items() if getattr(rate_set, name) != value}
    if not values:
        return rate_set
    before = snapshot(rate_set, SET_FIELDS)
    try:
        with transaction.atomic():
            rate_set.versioned_update(user, **values)
    except IntegrityError:
        raise _name_conflict() from None
    changed_before, changed_after = changes(before, snapshot(rate_set, SET_FIELDS))
    record("pricing.market_rate_set_updated", obj=rate_set, actor=user, before=changed_before, after=changed_after)
    bump(AUTHORING_NAMESPACE)
    return rate_set


@transaction.atomic
def delete_set(instance: MarketRateSet, *, user, expected_version=None) -> None:
    rate_set = _locked_set(instance, expected_version, editable=False)
    if rate_set.status == MarketRateSetStatus.ACTIVE:
        raise Conflict("market_rate_set_active", "The ACTIVE set cannot be deleted; activate another set first.")
    if PriceRelease.objects.filter(market_rate_set=rate_set).exists():
        raise Conflict("market_rate_set_released", "A released set is kept forever (releases reference it).")
    rate_set.soft_delete(user)
    record("pricing.market_rate_set_deleted", obj=rate_set, actor=user, before=snapshot(rate_set, SET_FIELDS))
    bump(AUTHORING_NAMESPACE)


@transaction.atomic
def activate_set(instance: MarketRateSet, *, user, expected_version=None, note: str = "") -> MarketRateSet:
    rate_set = _locked_set(instance, expected_version, editable=False)
    if rate_set.status == MarketRateSetStatus.ACTIVE:
        raise Conflict("market_rate_set_already_active", "This set is already ACTIVE.")
    if not MarketRate.objects.filter(set=rate_set).exists():
        raise Conflict("market_rate_set_empty", "A set without market rates cannot become ACTIVE.")
    now = timezone.now()
    previous = MarketRateSet.objects.select_for_update().filter(status=MarketRateSetStatus.ACTIVE).exclude(pk=rate_set.pk).first()
    if previous is not None:
        previous.versioned_update(user, status=MarketRateSetStatus.RETIRED, retired_at=now)
        record("pricing.market_rate_set_retired", obj=previous, actor=user, after={"status": MarketRateSetStatus.RETIRED, "replaced_by": str(rate_set.uid)})
    before = rate_set.status
    try:
        with transaction.atomic():
            rate_set.versioned_update(user, status=MarketRateSetStatus.ACTIVE, activated_at=now, retired_at=None)
    except IntegrityError:
        raise Conflict("market_rate_set_activation_conflict", "Another set was activated at the same time; reload.") from None
    record("pricing.market_rate_set_activated", obj=rate_set, actor=user, before={"status": before}, after={"status": MarketRateSetStatus.ACTIVE}, note=note)
    emit(
        "pricing.market_rate_set_activated",
        {"set_uid": str(rate_set.uid), "previous_set_uid": str(previous.uid) if previous else None},
        aggregate_type="pricing.market_rate_set",
        aggregate_uid=rate_set.uid,
    )
    bump(AUTHORING_NAMESPACE)
    return rate_set


# ── children (bulk PUT) ────────────────────────────────────────────────────────────────────────────────────────────


def normalise_rate(values: dict, index: int) -> tuple[dict, dict]:
    """Validate one rate row; returns ``(clean values, errors)``. Derives ``size_kw``/``phase``/``from_size_kw``."""
    errors: dict[str, list[str]] = {}
    system = values.get("system_type")
    tier = values.get("tier") or ""
    battery = values.get("battery_config") or ""
    size = valid_size_key(values.get("size_key", ""))
    from_key = values.get("from_size_key") or ""
    future_key = values.get("future_size_key") or ""
    variant = values.get("variant") or ""
    if size is None:
        errors["size_key"] = [SIZE_KEY_HELP]
    if system == SystemType.UPGRADE:
        if valid_size_key(from_key) is None:
            errors["from_size_key"] = [f"UPGRADE rows need the starting size key ({SIZE_KEY_HELP})"]
        if battery or future_key or variant:
            errors["system_type"] = ["UPGRADE rows carry no battery band, future size or variant."]
    else:
        if not tier:
            errors["tier"] = ["Required for ON-GRID and HYBRID rows."]
        if from_key:
            errors["from_size_key"] = ["Only UPGRADE rows have a starting size."]
        if system == SystemType.HYBRID and battery not in ("0", "1", "2"):
            errors["battery_config"] = ["HYBRID rows need a battery band (0, 1 or 2)."]
        if system == SystemType.ONGRID and battery:
            errors["battery_config"] = ["ON-GRID rows have no battery band."]
    if future_key and valid_size_key(future_key) is None:
        errors["future_size_key"] = [SIZE_KEY_HELP]
    if variant and not VARIANT_RE.match(variant):
        errors["variant"] = ["Letters and digits only (≤ 24)."]
    price = values.get("customer_price_incl_gst")
    if price is None or Decimal(price) < 0:
        errors["customer_price_incl_gst"] = ["Must be ≥ 0 (0 = not set)."]
    if errors:
        return {}, {f"rates[{index}].{field}": messages for field, messages in errors.items()}
    clean = {
        "system_type": system,
        "tier": tier,
        "battery_config": battery,
        "size_key": values["size_key"],
        "size_kw": size[0],
        "phase": size[1],
        "from_size_key": from_key,
        "from_size_kw": valid_size_key(from_key)[0] if from_key else None,
        "future_size_key": future_key,
        "variant": variant,
        "customer_price_incl_gst": Decimal(price).quantize(Decimal("0.01")),
        "sort_order": values.get("sort_order", index),
    }
    return clean, {}


def _replace(model, rate_set: MarketRateSet, rows: list[dict], key_of, fields, *, user, created_at=None) -> dict:
    """Upsert ``rows`` (column values) into the set's ``model`` children by natural key; soft-delete the rest.

    ``key_of(mapping)`` builds the natural key from a values dict; existing rows are keyed through the same function
    applied to their column values.
    """
    existing = {key_of({name: getattr(row, name) for name in fields}): row for row in model.objects.filter(set=rate_set)}
    seen = set()
    outcome = {"created": 0, "updated": 0, "unchanged": 0, "deleted": 0}
    for values in rows:
        key = key_of(values)
        if key in seen:
            raise DomainError("validation_error", "The same row appears twice.", errors={"rows": [f"Duplicate: {key}"]})
        seen.add(key)
        current = existing.get(key)
        if current is None:
            instance = model(set=rate_set, **{name: values[name] for name in fields if name in values})
            stamp_create(instance, user)
            if created_at is not None:
                instance.created_at = instance.updated_at = created_at
            instance.save()
            outcome["created"] += 1
            continue
        diff = {name: values[name] for name in fields if name in values and getattr(current, name) != values[name]}
        if diff:
            current.versioned_update(user, **diff)
            outcome["updated"] += 1
        else:
            outcome["unchanged"] += 1
    for key, row in existing.items():
        if key not in seen:
            row.soft_delete(user)
            outcome["deleted"] += 1
    return outcome


RATE_FIELDS = ("system_type", "tier", "battery_config", "size_kw", "size_key", "phase", "from_size_key", "from_size_kw", "future_size_key", "variant", "customer_price_incl_gst", "sort_order")


def _finish_child_write(rate_set: MarketRateSet, outcome: dict, *, user, action: str) -> None:
    if outcome["created"] or outcome["updated"] or outcome["deleted"]:
        rate_set.versioned_update(user)
        record(action, obj=rate_set, actor=user, after=outcome)
        bump(AUTHORING_NAMESPACE)


@transaction.atomic
def put_rates(instance: MarketRateSet, *, user, rows: list[dict], expected_version=None) -> dict:
    rate_set = _locked_set(instance, expected_version)
    clean_rows, errors = [], {}
    for index, values in enumerate(rows):
        clean, problems = normalise_rate(values, index)
        errors.update(problems)
        clean_rows.append(clean)
    if errors:
        raise DomainError("validation_error", "Invalid market rates.", errors=errors)
    outcome = _replace(MarketRate, rate_set, clean_rows, natural_key, RATE_FIELDS, user=user)
    _finish_child_write(rate_set, outcome, user=user, action="pricing.market_rates_replaced")
    return outcome


DELTA_FIELDS = ("system_type", "tier", "slot", "from_component_id", "to_component_id", "delta_incl_gst")
ADDON_FIELDS = ("structure_type", "size_kw", "addon_incl_gst")


def _delta_key(values: dict) -> tuple:
    return (values["system_type"], values["tier"], values["slot"], values["from_component_id"], values["to_component_id"])


def _addon_key(values: dict) -> tuple:
    return (values["structure_type"], Decimal(values["size_kw"]).normalize())


@transaction.atomic
def put_swap_deltas(instance: MarketRateSet, *, user, rows: list[dict], expected_version=None) -> dict:
    """Rows: ``{system_type, tier, slot, from_component, to_component, delta_incl_gst}`` (components are instances)."""
    rate_set = _locked_set(instance, expected_version)
    errors, clean = {}, []
    for index, values in enumerate(rows):
        if values["from_component"].pk == values["to_component"].pk:
            errors[f"swap_deltas[{index}].to_component_uid"] = ["A swap needs two different components."]
        clean.append(
            {
                "system_type": values["system_type"],
                "tier": values["tier"],
                "slot": values["slot"],
                "from_component_id": values["from_component"].pk,
                "to_component_id": values["to_component"].pk,
                "delta_incl_gst": Decimal(values["delta_incl_gst"]).quantize(Decimal("0.01")),
            }
        )
    if errors:
        raise DomainError("validation_error", "Invalid swap deltas.", errors=errors)
    outcome = _replace(SwapDelta, rate_set, clean, _delta_key, DELTA_FIELDS, user=user)
    _finish_child_write(rate_set, outcome, user=user, action="pricing.swap_deltas_replaced")
    return outcome


@transaction.atomic
def put_roof_addons(instance: MarketRateSet, *, user, rows: list[dict], expected_version=None) -> dict:
    rate_set = _locked_set(instance, expected_version)
    clean = [
        {"structure_type": values["structure_type"], "size_kw": Decimal(values["size_kw"]).quantize(Decimal("0.01")), "addon_incl_gst": Decimal(values["addon_incl_gst"]).quantize(Decimal("0.01"))}
        for values in rows
    ]
    outcome = _replace(RoofAddon, rate_set, clean, _addon_key, ADDON_FIELDS, user=user)
    _finish_child_write(rate_set, outcome, user=user, action="pricing.roof_addons_replaced")
    return outcome


def rate_summary(rate: MarketRate) -> dict:
    return {
        "key": rate_key(rate),
        "system_type": rate.system_type,
        "tier": rate.tier,
        "battery_config": rate.battery_config,
        "size_key": rate.size_key,
        "size_kw": decimal_text(rate.size_kw),
        "phase": rate.phase,
        "from_size_key": rate.from_size_key,
        "future_size_key": rate.future_size_key,
        "variant": rate.variant,
        "customer_price_incl_gst": format(rate.customer_price_incl_gst, "f"),
    }
