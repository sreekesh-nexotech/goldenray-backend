"""Pack config versions (PLAN §2.5, §3.4): draft → submit → approve / reject, pins, checker runs.

Lifecycle (``packs_config_version.status``; the Flarize ``packConfig.js`` store as rows):

* ``create_draft`` — a new DRAFT copied from the current approved version (or ``based_on`` another version with a
  configuration; the first one may bring its own ``config``). One open draft (DRAFT/SUBMITTED) at a time: partial
  unique index, 409 ``draft_exists``.
* ``update_draft`` — replaces the whole ``config`` or some ``sections``; validated by ``engines.pack_config.schema``
  (400 ``invalid_config``). Editing a SUBMITTED draft withdraws the submission (back to DRAFT), as in Flarize.
  ``change_log`` gets ``{at, by, section, note, changed}`` per section.
* ``set_pins`` — ``PUT …/packs/<key>/``: the pack's pinned components (the platform's replacement of the Flarize
  package registry); each must be selectable (``catalog.assert_selectable``) and of the slot's category.
* ``submit`` (``packs.submit``) — DRAFT → SUBMITTED; 409 ``no_changes`` when configuration and pins equal the current
  approved version (Flarize ``submitDraft``).
* ``approve`` (``packs.approve``) — SUBMITTED → APPROVED (``direct=true`` approves a DRAFT in one step, recorded as
  submitted and approved by the approver — Flarize's Admin path); the previous APPROVED/PUBLISHED version becomes
  SUPERSEDED in the same transaction (one current version: partial unique index).
* ``reject`` (``packs.approve``) — SUBMITTED → REJECTED with a reason (a closed version; a new draft may be based on it).
* ``run_checker`` (``engineering.verify``) — every pack through the engineering checker with the ACTIVE rule set,
  stored as one ``engineering_run`` (subject PACK_CONFIG_VERSION).

Every write: one transaction, compare-and-swap on ``version`` (``expected_version`` → 409 ``stale_version``), audit
``packs.config_*``, cache namespace ``packs:authoring`` bumped, outbox ``packs.config_*``, the typed mirror rebuilt.
"""

from __future__ import annotations

import copy

from django.db import IntegrityError, transaction
from django.db.models import Max
from django.utils import timezone

from audit.services import record
from catalog.models import Component
from catalog.services import assert_selectable
from core.errors import Conflict, DomainError, NotFound
from core.outbox import emit
from core.services import check_version, stamp_create
from engineering.models import Engine, SubjectType
from engineering.services import rule_sets, runs
from engines.pack_config import CONFIG_SECTIONS, PackConfigError, validate_config
from flarize.cache_utils import bump
from packs.models import CURRENT_STATUSES, OPEN_STATUSES, ConfigPack, ConfigPin, ConfigStatus, ConfigVersion
from packs.services import engine, mirror
from packs.services.context import engine_context

AUTHORING_NAMESPACE = "packs:authoring"


def versions_queryset():
    return ConfigVersion.objects.select_related("based_on", "submitted_by", "approved_by", "rejected_by")


def current_version() -> ConfigVersion | None:
    return ConfigVersion.objects.filter(status__in=CURRENT_STATUSES).first()


def open_draft() -> ConfigVersion | None:
    return ConfigVersion.objects.filter(status__in=OPEN_STATUSES).first()


def _actor_label(user) -> str:
    return str(getattr(user, "uid", "") or "system")


def _locked(version: ConfigVersion, expected_version=None) -> ConfigVersion:
    locked = ConfigVersion.objects.select_for_update().get(pk=version.pk)
    check_version(locked, expected_version)
    return locked


def _editable(version: ConfigVersion) -> None:
    if version.status not in OPEN_STATUSES:
        raise Conflict("version_not_editable", f"Only an open draft is edited; v{version.number} is {version.status}.", errors={"status": [version.status]})


def validate(config) -> dict:
    try:
        validate_config(config)
    except PackConfigError as exc:
        field = f"config.{exc.detail.get('section')}" if isinstance(exc.detail, dict) and exc.detail.get("section") else "config"
        raise DomainError("invalid_config", exc.message, errors={field: [exc.message]}) from None
    return config


def _pins_signature(version: ConfigVersion) -> list:
    rows = ConfigPin.objects.filter(pack__config_version=version, pack__deleted_at__isnull=True).values_list("pack__key", "slot_key", "component__sku", "authoritative", "alternates")
    return sorted((key, slot, sku, bool(auth), list(alternates or [])) for key, slot, sku, auth, alternates in rows)


def _copy_pins(source: ConfigVersion, target: ConfigVersion, *, user) -> int:
    """Create the target's packs (keys only) and copy the source's pins; the mirror fills the packs in."""
    count = 0
    for pack in ConfigPack.objects.filter(config_version=source).prefetch_related("pins"):
        pins = [pin for pin in pack.pins.all()]
        if not pins:
            continue
        clone = ConfigPack(
            config_version=target,
            key=pack.key,
            system_type=pack.system_type,
            tier=pack.tier,
            size_key=pack.size_key,
            size_kw=pack.size_kw,
            phase=pack.phase,
            battery_config=pack.battery_config,
            future_size_key=pack.future_size_key,
            future_size_kw=pack.future_size_kw,
            is_future_ready=pack.is_future_ready,
            sort_order=pack.sort_order,
        )
        stamp_create(clone, user)
        clone.save()
        for pin in pins:
            copied = ConfigPin(pack=clone, slot_key=pin.slot_key, component_id=pin.component_id, authoritative=pin.authoritative, alternates=list(pin.alternates or []))
            stamp_create(copied, user)
            copied.save()
            count += 1
    return count


def _events(action: str, version: ConfigVersion, *, user, before=None, after=None, note: str = "", payload: dict | None = None) -> None:
    record(f"packs.config_{action}", obj=version, actor=user, before=before, after={"number": version.number, "status": version.status, **(after or {})}, note=note)
    emit(
        f"packs.config_{action}",
        {"version_uid": str(version.uid), "number": version.number, "status": version.status, **(payload or {})},
        aggregate_type="packs.config_version",
        aggregate_uid=version.uid,
    )
    bump(AUTHORING_NAMESPACE)


@transaction.atomic
def create_draft(*, user, data: dict) -> ConfigVersion:
    if ConfigVersion.objects.select_for_update().filter(status__in=OPEN_STATUSES).exists():
        raise Conflict("draft_exists", "An open draft already exists; edit or resolve it first.")
    source = data.get("based_on") or current_version()
    config = data.get("config")
    if config is None:
        if source is None:
            raise DomainError("validation_error", "There is no approved version to copy; send a config.", errors={"config": ["Required: no approved version exists yet."]})
        if source.config is None:
            raise Conflict("based_on_has_no_config", f"v{source.number} has no configuration to copy (imported history).")
        config = copy.deepcopy(source.config)
    validate(config)
    number = (ConfigVersion.all_objects.aggregate(top=Max("number"))["top"] or 0) + 1
    version = ConfigVersion(number=number, status=ConfigStatus.DRAFT, based_on=source, config=config, note=data.get("note", "") or "")
    version.change_log = [{"at": timezone.now().isoformat(), "by": _actor_label(user), "section": "*", "note": f"created from v{source.number}" if source else "created", "changed": False}]
    stamp_create(version, user)
    try:
        with transaction.atomic():
            version.save()
    except IntegrityError:
        raise Conflict("draft_exists", "An open draft was created at the same time; reload.") from None
    pins = _copy_pins(source, version, user=user) if source is not None and data.get("config") is None else 0
    mirrored = mirror.mirror(version, user=user)
    _events(
        "draft_created", version, user=user, after={"based_on": source.number if source else None, "pins": pins, "mirror": mirrored}, payload={"based_on_number": source.number if source else None}
    )
    return version


@transaction.atomic
def update_draft(version: ConfigVersion, *, user, data: dict, expected_version=None) -> ConfigVersion:
    locked = _locked(version, expected_version)
    _editable(locked)
    note = data.get("note", "") or ""
    new = copy.deepcopy(locked.config)
    if data.get("config") is not None:
        new = copy.deepcopy(data["config"])
    for section, value in (data.get("sections") or {}).items():
        if section not in CONFIG_SECTIONS:
            raise DomainError("invalid_config", f'Unknown config section "{section}".', errors={f"sections.{section}": [f"Unknown section; allowed: {', '.join(CONFIG_SECTIONS)}."]})
        new[section] = copy.deepcopy(value)
    validate(new)
    now = timezone.now().isoformat()
    changed = [section for section in CONFIG_SECTIONS if (locked.config or {}).get(section) != new.get(section)]
    log = list(locked.change_log or [])
    for section in changed or ["*"]:
        log.append({"at": now, "by": _actor_label(user), "section": section, "note": note, "changed": bool(changed)})
    values = {"config": new, "change_log": log}
    withdrawn = locked.status == ConfigStatus.SUBMITTED
    if withdrawn:
        values.update(status=ConfigStatus.DRAFT, submitted_by=None, submitted_at=None)
    locked.versioned_update(user, **values)
    mirrored = mirror.mirror(locked, user=user)
    _events("draft_updated", locked, user=user, after={"sections": changed, "withdrawn": withdrawn, "mirror": mirrored}, note=note, payload={"sections": changed})
    return locked


def get_pack(version: ConfigVersion, key: str) -> ConfigPack:
    pack = ConfigPack.objects.filter(config_version=version, key=key).first()
    if pack is None:
        raise NotFound("pack_not_found", f"v{version.number} has no pack {key!r}.")
    return pack


@transaction.atomic
def set_pins(version: ConfigVersion, key: str, *, user, pins: dict, expected_version=None, note: str = "") -> ConfigPack:
    locked = _locked(version, expected_version)
    _editable(locked)
    pack = get_pack(locked, key)
    template = ((locked.config or {}).get("bomTemplates") or {}).get(pack.system_type.lower()) or {}
    slots = {slot.get("category") for slot in template.get("slots") or [] if slot.get("variable", True)}
    errors: dict[str, list[str]] = {}
    resolved: dict[str, Component] = {}
    for slot_key, component_uid in (pins or {}).items():
        if slot_key not in slots:
            errors[f"pins.{slot_key}"] = [f"Not a variable slot of the {pack.system_type} template."]
            continue
        if component_uid is None:
            continue
        component = Component.all_objects.filter(uid=component_uid).select_related("category").first()
        if component is None:
            errors[f"pins.{slot_key}"] = ["Unknown component."]
            continue
        try:
            assert_selectable(component, field=f"pins.{slot_key}")
        except DomainError as exc:
            errors.update({key_: list(messages) for key_, messages in (exc.errors or {f"pins.{slot_key}": [exc.message]}).items()})
            continue
        if component.category.slug != slot_key:
            errors[f"pins.{slot_key}"] = [f"{component.sku} is a {component.category.slug}, not a {slot_key}."]
            continue
        resolved[slot_key] = component
    if errors:
        raise DomainError("validation_error", "Invalid pins.", errors=errors)
    existing = {pin.slot_key: pin for pin in ConfigPin.objects.filter(pack=pack)}
    for slot_key, pin in existing.items():
        if slot_key not in resolved:
            pin.soft_delete(user)
    for slot_key, component in resolved.items():
        pin = existing.get(slot_key)
        if pin is None:
            pin = ConfigPin(pack=pack, slot_key=slot_key, component=component, authoritative=True)
            stamp_create(pin, user)
            pin.save()
        elif pin.component_id != component.pk or not pin.authoritative:
            pin.versioned_update(user, component=component, authoritative=True)
    log = [*(locked.change_log or []), {"at": timezone.now().isoformat(), "by": _actor_label(user), "section": f"pins:{key}", "note": note, "changed": True}]
    values = {"change_log": log}
    if locked.status == ConfigStatus.SUBMITTED:
        values.update(status=ConfigStatus.DRAFT, submitted_by=None, submitted_at=None)
    locked.versioned_update(user, **values)
    mirror.mirror(locked, user=user)
    _events("pins_updated", locked, user=user, after={"pack": key, "pins": {slot: c.sku for slot, c in resolved.items()}}, note=note, payload={"pack": key})
    return get_pack(locked, key)


def _refuse_unchanged(version: ConfigVersion, verb: str) -> None:
    """409 ``no_changes`` when configuration and pins equal the current approved version (Flarize ``NO_CHANGES``)."""
    approved = current_version()
    if approved is None or approved.pk == version.pk:
        return
    if approved.config == version.config and _pins_signature(approved) == _pins_signature(version):
        raise Conflict("no_changes", f"v{version.number} is identical to the approved v{approved.number}; nothing to {verb}.")


@transaction.atomic
def submit(version: ConfigVersion, *, user, expected_version=None) -> ConfigVersion:
    locked = _locked(version, expected_version)
    if locked.status == ConfigStatus.SUBMITTED:
        raise Conflict("already_submitted", f"v{locked.number} is already submitted.")
    if locked.status != ConfigStatus.DRAFT:
        raise Conflict("version_not_draft", f"Only a DRAFT is submitted; v{locked.number} is {locked.status}.")
    _refuse_unchanged(locked, "submit")
    locked.versioned_update(user, status=ConfigStatus.SUBMITTED, submitted_by=user, submitted_at=timezone.now())
    _events("submitted", locked, user=user)
    return locked


@transaction.atomic
def approve(version: ConfigVersion, *, user, expected_version=None, note: str = "", direct: bool = False) -> ConfigVersion:
    locked = _locked(version, expected_version)
    now = timezone.now()
    values = {"status": ConfigStatus.APPROVED, "approved_by": user, "approved_at": now, "note": note or locked.note}
    if locked.status == ConfigStatus.DRAFT and direct:
        _refuse_unchanged(locked, "approve")
        values.update(submitted_by=user, submitted_at=now)
    elif locked.status != ConfigStatus.SUBMITTED:
        raise Conflict("version_not_submitted", f"Only a SUBMITTED version is approved (or a DRAFT with direct=true); v{locked.number} is {locked.status}.")
    previous = ConfigVersion.objects.select_for_update().filter(status__in=CURRENT_STATUSES).exclude(pk=locked.pk).first()
    if previous is not None:
        previous.versioned_update(user, status=ConfigStatus.SUPERSEDED, superseded_at=now)
        record("packs.config_superseded", obj=previous, actor=user, after={"number": previous.number, "superseded_by": locked.number})
    try:
        with transaction.atomic():
            locked.versioned_update(user, **values)
    except IntegrityError:
        raise Conflict("approve_conflict", "Another version was approved at the same time; reload.") from None
    _events("approved", locked, user=user, after={"direct": direct, "superseded": previous.number if previous else None}, note=note, payload={"previous_number": previous.number if previous else None})
    return locked


@transaction.atomic
def reject(version: ConfigVersion, *, user, reason: str, expected_version=None) -> ConfigVersion:
    locked = _locked(version, expected_version)
    reason = (reason or "").strip()
    if not reason:
        raise DomainError("validation_error", "A reason is required.", errors={"reason": ["This field may not be blank."]})
    if locked.status != ConfigStatus.SUBMITTED:
        raise Conflict("version_not_submitted", f"Only a SUBMITTED version is rejected; v{locked.number} is {locked.status}.")
    locked.versioned_update(user, status=ConfigStatus.REJECTED, rejected_by=user, rejected_at=timezone.now(), rejection_reason=reason)
    _events("rejected", locked, user=user, after={"reason": reason}, note=reason)
    return locked


def check_version_packs(version: ConfigVersion, *, ctx=None, at: str | None = None, rule_set=None) -> list[tuple[str, object]]:
    """``[(pack key, CheckResult)]`` for every pack the BOM builder accepts (no write)."""
    ctx = ctx or engine_context()
    rule_set = rule_set or rule_sets.active_rule_set(Engine.CHECKER)
    parsed = rule_sets.engine_rule_set(rule_set)
    at = at or timezone.now().isoformat()
    from packs.services.context import version_pins

    pins = version_pins(version)
    env = {"catalog": engine.checker_catalog(ctx.catalog, version.config), "batteryMaster": ctx.battery_master, "catalogVersion": ctx.price_release.number if ctx.price_release else None}
    results = []
    for spec in engine.enumerate_packs(version.config):
        try:
            bom = engine.build(spec, config=version.config, catalog=ctx.catalog, pins=pins.get(spec.key, []))
            results.append((spec.key, engine.check(spec, bom, checker_env=env, rule_set=parsed, at=at)))
        except engine.EngineError:
            continue
    return results


@transaction.atomic
def run_checker(version: ConfigVersion, *, user):
    if version.config is None:
        raise Conflict("version_has_no_config", f"v{version.number} has no configuration (imported history).")
    rule_set = rule_sets.active_rule_set(Engine.CHECKER)
    checks = check_version_packs(version, rule_set=rule_set)
    run = runs.record_run(rule_set=rule_set, subject_type=SubjectType.PACK_CONFIG_VERSION, subject_uid=version.uid, subject_label=f"Pack config v{version.number}", checks=checks, user=user)
    record("packs.config_checked", obj=version, actor=user, after={"number": version.number, "run": str(run.uid), "result": run.result})
    return run
