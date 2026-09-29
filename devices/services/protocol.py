"""``devices/protocol-mappings/`` — meanings of raw terminal codes (module ``devices``).

A mapping is scoped to a platform and firmware (empty = any) and unique per ``(platform, firmware, field, raw value)``
among live rows (409 ``protocol_mapping_exists``). ``observed/`` lists the raw ``status``/``punch`` codes the punch
store has actually seen, with counts and what each is mapped to — the evidence base for settling a code's meaning
(eSSL spec, "OPEN PROTOCOL MAPPING ITEM"). The raw codes in stored punches never change.
"""

from __future__ import annotations

from django.db import IntegrityError, transaction

from audit.services import changes, record, snapshot
from core.services import stamp_create
from devices.models import ProtocolMapping
from devices.services import punch_sink
from devices.services.common import NS_PROTOCOL, bump_devices, lock, unique_conflict

FIELDS = ("device_platform", "firmware_version", "field", "raw_value", "meaning_type", "meaning_code", "label", "confidence", "notes")
UNIQUE = {"devices_protocol_mapping_scope_live_uniq": ("protocol_mapping_exists", "raw_value", "This code already has a mapping for this platform and firmware.")}


def mappings_queryset():
    return ProtocolMapping.objects.order_by("field", "raw_value", "device_platform", "firmware_version", "id")


def _normalise(values: dict) -> dict:
    for name in ("device_platform", "firmware_version", "label", "notes"):
        if name in values:
            values[name] = (values[name] or "").strip()
    if "meaning_code" in values:
        values["meaning_code"] = (values["meaning_code"] or "").strip().upper()
    return values


def _save(action):
    try:
        with transaction.atomic():
            action()
    except IntegrityError as exc:
        raise (unique_conflict(exc, UNIQUE) or exc) from None


@transaction.atomic
def create_mapping(*, user, data) -> ProtocolMapping:
    values = _normalise({name: data[name] for name in FIELDS if name in data})
    mapping = ProtocolMapping(**values)
    stamp_create(mapping, user)
    _save(mapping.save)
    record("devices.protocol_mapping_created", obj=mapping, actor=user, after=snapshot(mapping, FIELDS))
    bump_devices(NS_PROTOCOL)
    return mapping


@transaction.atomic
def update_mapping(instance: ProtocolMapping, *, user, data, expected_version=None) -> ProtocolMapping:
    mapping = lock(ProtocolMapping, instance, expected_version)
    before = snapshot(mapping, FIELDS)
    values = _normalise({name: data[name] for name in FIELDS if name in data})
    values = {name: value for name, value in values.items() if getattr(mapping, name) != value}
    if not values:
        return mapping
    _save(lambda: mapping.versioned_update(user, **values))
    changed_before, changed_after = changes(before, snapshot(mapping, FIELDS))
    record("devices.protocol_mapping_updated", obj=mapping, actor=user, before=changed_before, after=changed_after)
    bump_devices(NS_PROTOCOL)
    return mapping


@transaction.atomic
def delete_mapping(instance: ProtocolMapping, *, user, expected_version=None) -> None:
    mapping = lock(ProtocolMapping, instance, expected_version)
    mapping.soft_delete(user)
    record("devices.protocol_mapping_deleted", obj=mapping, actor=user, before=snapshot(mapping, FIELDS))
    bump_devices(NS_PROTOCOL)


def _meaning(mappings: list[ProtocolMapping], field: str, raw_value, platform: str, firmware: str):
    """The most specific mapping: exact platform + firmware, then platform only, then the wildcard row."""
    candidates = [row for row in mappings if row.field == field and row.raw_value == raw_value]
    for want_platform, want_firmware in ((platform, firmware), (platform, ""), ("", "")):
        for row in candidates:
            if row.device_platform == want_platform and row.firmware_version == want_firmware:
                return row
    return None


def observed() -> dict:
    """``{"status": [...], "punch": [...], "available": bool}`` — observed codes with counts and their mapping."""
    rows = punch_sink.get().observed_codes()
    mappings = list(mappings_queryset())
    result = {"status": [], "punch": [], "available": punch_sink.installed()}
    for item in sorted(rows, key=lambda entry: (-int(entry.get("count") or 0), str(entry.get("raw_value")))):
        field = item.get("field")
        if field not in result or field == "available":
            continue
        platform, firmware = item.get("device_platform") or "", item.get("firmware_version") or ""
        mapping = _meaning(mappings, field, item.get("raw_value"), platform, firmware)
        result[field].append(
            {
                "raw_value": item.get("raw_value"),
                "count": int(item.get("count") or 0),
                "device_platform": platform,
                "firmware_version": firmware,
                "mapped_to": mapping.meaning_code if mapping else None,
                "confidence": mapping.confidence if mapping else None,
                "mapping_uid": mapping.uid if mapping else None,
            }
        )
    return result
