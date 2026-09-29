"""``devices/`` — terminals (module ``devices``): validated CRUD, rehome, ADMS enable/disable, read requests, mapping.

* Every write is validated field by field (eSSL's PATCH was an unvalidated ``setattr``, spec §I.24): serials are
  trimmed, MACs normalised to ``aa:bb:cc:dd:ee:ff`` (anything else is 400), addresses must be IP addresses, the office
  and the agent must exist (and the agent be active). A new device needs an address or the serial it will announce
  (400 ``device_not_locatable``); ``expected_serial`` defaults to the typed serial. A serial another device reports or
  is pinned to is 409 ``device_serial_taken``.
* The hardware-reported fields (``serial_number``, ``mac_address``, model, firmware, platform, contact timestamps,
  counters) are written only by the agent protocol and the ADMS receiver — never by staff.
* The agent binding changes only through ``rehome/`` (``devices.manage``): an agent never moves a device bound to
  another one (409 ``device_bound_elsewhere`` in the agent protocol).
* ``adms/enable/`` (``manage``) issues the per-device path token for ``/iclock/<device_token>/…`` — shown once, only
  its sha256 is stored — and sets the optional source-address allow-list; ``adms/disable/`` withdraws it.
* ``refresh-employees/`` (``sync``) cannot dial a terminal (the server is on no office LAN): it asks the device's
  agent to re-read and upload the user table on its next cycle (``users_read_requested_at``) and answers from the
  last successful read.
* A device with history (device users, punches) cannot be deleted (409 ``device_has_history``): deactivate it.
"""

from __future__ import annotations

import hashlib
import secrets

from django.db import IntegrityError, transaction
from django.db.models import Q

from audit.services import changes, record, snapshot
from core.errors import Conflict, DomainError
from core.services import stamp_create
from devices.models import Agent, Device, DeviceUser
from devices.services import health, punch_sink, roster
from devices.services.common import NS_DEVICE_USERS, bump_devices, lock, normalize_mac, normalize_networks, normalize_serial, now, unique_conflict

CREATE_FIELDS = (
    "name",
    "serial_number",
    "expected_serial",
    "expected_mac",
    "office",
    "agent",
    "ip_address",
    "port",
    "protocol",
    "comm_password",
    "timeout_seconds",
    "adms_server",
    "adms_port",
    "notes",
    "is_active",
)
EDITABLE_FIELDS = ("name", "expected_serial", "expected_mac", "office", "ip_address", "port", "protocol", "comm_password", "timeout_seconds", "adms_server", "adms_port", "notes", "is_active")
SNAPSHOT_FIELDS = (*CREATE_FIELDS, "adms_enabled", "adms_allowed_ips")
UNIQUE = {
    "devices_device_serial_live_uniq": ("device_serial_taken", "serial_number", "Another device already reports this serial."),
    "devices_device_expected_serial_live_uniq": ("device_serial_taken", "expected_serial", "Another device is already pinned to this serial."),
}
MAPPING_REPORT_LIMIT = 1000


def devices_queryset():
    return Device.objects.select_related("office", "agent__office", "agent__credential").order_by("name", "id")


def device_snapshot(device: Device) -> dict:
    return snapshot(device, SNAPSHOT_FIELDS)


def _invalid(field: str, message: str) -> DomainError:
    return DomainError("validation_error", "Invalid input.", errors={field: [message]})


def _normalise(values: dict) -> dict:
    if "name" in values:
        values["name"] = (values["name"] or "").strip()
        if not values["name"]:
            raise _invalid("name", "This field may not be blank.")
    for name in ("serial_number", "expected_serial"):
        if name in values:
            values[name] = normalize_serial(values[name])
    if "expected_mac" in values:
        raw = (values["expected_mac"] or "").strip()
        values["expected_mac"] = normalize_mac(raw)
        if raw and not values["expected_mac"]:
            raise _invalid("expected_mac", "Not a MAC address (12 hexadecimal digits, separators optional).")
    for name in ("notes", "adms_server"):
        if name in values:
            values[name] = (values[name] or "").strip() if name == "adms_server" else (values[name] or "")
    if "agent" in values and values["agent"] is not None and not values["agent"].is_active:
        raise _invalid("agent", "This agent is revoked or disabled.")
    return values


def _serial_owner(serial: str | None, exclude_pk=None) -> Device | None:
    """Another live device that reports or is pinned to ``serial``."""
    if not serial:
        return None
    others = Device.objects.filter(Q(serial_number=serial) | Q(expected_serial=serial))
    if exclude_pk is not None:
        others = others.exclude(pk=exclude_pk)
    return others.first()


def _check_serials(values: dict, device: Device | None = None) -> None:
    for name in ("serial_number", "expected_serial"):
        owner = _serial_owner(values.get(name), device.pk if device else None)
        if owner is not None:
            raise Conflict("device_serial_taken", f"{owner.name} already reports or is pinned to serial {values[name]}.", errors={name: ["Already in use."]})


def _save(action):
    try:
        with transaction.atomic():
            action()
    except IntegrityError as exc:
        raise (unique_conflict(exc, UNIQUE) or exc) from None


@transaction.atomic
def create_device(*, user, data) -> Device:
    values = _normalise({name: data[name] for name in CREATE_FIELDS if name in data})
    if values.get("serial_number") and not values.get("expected_serial"):
        values["expected_serial"] = values["serial_number"]  # registering from the label fixes what the terminal must report
    if not values.get("ip_address") and not values.get("expected_serial"):
        raise DomainError(
            "device_not_locatable",
            "Give an IP address, or the serial on the label so an office agent can discover the terminal on its LAN.",
            errors={"ip_address": ["An address or an expected serial is required."], "expected_serial": ["An address or an expected serial is required."]},
        )
    _check_serials(values)
    device = Device(**values)
    stamp_create(device, user)
    _save(device.save)
    record("devices.device_created", obj=device, actor=user, after=device_snapshot(device))
    bump_devices()
    return device


@transaction.atomic
def update_device(instance: Device, *, user, data, expected_version=None) -> Device:
    device = lock(Device, instance, expected_version)
    before = device_snapshot(device)
    values = _normalise({name: data[name] for name in EDITABLE_FIELDS if name in data})
    values = {name: value for name, value in values.items() if getattr(device, name) != value}
    if not values:
        return device
    if not values.get("ip_address", device.ip_address) and not values.get("expected_serial", device.expected_serial) and not device.serial_number:
        raise DomainError("device_not_locatable", "A device keeps an address or an expected serial.", errors={"ip_address": ["An address or an expected serial is required."]})
    _check_serials({"expected_serial": values.get("expected_serial")}, device)
    _save(lambda: device.versioned_update(user, **values))
    changed_before, changed_after = changes(before, device_snapshot(device))
    record("devices.device_updated", obj=device, actor=user, before=changed_before, after=changed_after)
    bump_devices(*([NS_DEVICE_USERS] if "is_active" in values else []))
    return device


def history_counts(device: Device) -> dict[str, int]:
    stored = punch_sink.get().status(device).get("stored_records")
    return {
        "device_users": DeviceUser.objects.filter(device=device).count(),
        "punches": int(stored if stored is not None else device.attendance_count),
    }


@transaction.atomic
def delete_device(instance: Device, *, user, expected_version=None) -> None:
    device = lock(Device, instance, expected_version)
    blocking = {name: count for name, count in history_counts(device).items() if count}
    if blocking:
        raise Conflict(
            "device_has_history",
            f"{device.name} has " + ", ".join(f"{count} {name.replace('_', ' ')}" for name, count in blocking.items()) + ". Deactivate the device instead.",
            errors={name: [str(count)] for name, count in blocking.items()},
        )
    before = device_snapshot(device)
    if device.adms_enabled or device.adms_token_hash:
        device.versioned_update(user, adms_enabled=False, adms_token_hash=None)
    device.soft_delete(user)
    record("devices.device_deleted", obj=device, actor=user, before=before)
    bump_devices()


@transaction.atomic
def rehome(instance: Device, *, user, agent: Agent | None, office=None, office_given: bool = False, reason: str = "", expected_version=None) -> Device:
    """Bind the device to ``agent`` (or to none) and optionally move it to ``office`` (``devices.manage``)."""
    device = lock(Device, instance, expected_version)
    if agent is not None and not agent.is_active:
        raise _invalid("agent_uid", "This agent is revoked or disabled.")
    reason = (reason or "").strip()
    if not reason:
        raise _invalid("reason", "Say why the device is moved (it is audited).")
    values = {}
    if device.agent_id != (agent.pk if agent else None):
        values["agent"] = agent
    if office_given and device.office_id != (office.pk if office else None):
        values["office"] = office
    if not values:
        return device
    before = device_snapshot(device)
    device.versioned_update(user, **values)
    changed_before, changed_after = changes(before, device_snapshot(device))
    record("devices.device_rehomed", obj=device, actor=user, before=changed_before, after=changed_after, note=reason)
    bump_devices()
    return device


def _token_hash(token: str) -> str:
    return hashlib.sha256(token.encode("utf-8")).hexdigest()


@transaction.atomic
def enable_adms(instance: Device, *, user, allowed_ips=None, expected_version=None) -> tuple[Device, str]:
    """Issue a new ``/iclock/<device_token>/`` token (any previous one stops working) and enable push."""
    device = lock(Device, instance, expected_version)
    networks = normalize_networks(allowed_ips) if allowed_ips is not None else list(device.adms_allowed_ips or [])
    token = secrets.token_urlsafe(24)
    device.versioned_update(user, adms_enabled=True, adms_token_hash=_token_hash(token), adms_allowed_ips=networks)
    record("devices.device_adms_enabled", obj=device, actor=user, after={"adms_enabled": True, "adms_allowed_ips": networks, "token_issued": True})
    bump_devices()
    return device, token


@transaction.atomic
def disable_adms(instance: Device, *, user, expected_version=None) -> Device:
    device = lock(Device, instance, expected_version)
    if device.adms_enabled or device.adms_token_hash:
        device.versioned_update(user, adms_enabled=False, adms_token_hash=None)
        record("devices.device_adms_disabled", obj=device, actor=user, after={"adms_enabled": False})
        bump_devices()
    return device


def device_for_token(token: str) -> Device | None:
    """The live device whose ``/iclock/`` token this is (lookup by sha256; the token itself is never stored)."""
    if not token or len(token) > 128:
        return None
    return Device.objects.select_related("office").filter(adms_token_hash=_token_hash(token)).first()


@transaction.atomic
def request_user_read(instance: Device, *, user) -> dict:
    """Ask the device's agent to re-read the terminal's user table on its next cycle (nothing is dialled from here)."""
    device = lock(Device, instance)
    if not device.is_active:
        raise Conflict("device_inactive", "This device is deactivated here. Reactivate it before refreshing its employees; its mappings and history stay visible in the reconciliation.")
    kind = health.transport(device)
    requested_at = None
    if kind == health.AGENT_DELIVERED:
        requested_at = now()
        Device.all_objects.filter(pk=device.pk).update(users_read_requested_at=requested_at)
        device.users_read_requested_at = requested_at
        record("devices.device_users_read_requested", obj=device, actor=user, after={"users_read_requested_at": requested_at})
        note = "The office agent re-reads this terminal's user table on its next cycle; this answer is the last successful read."
    elif kind == health.ADMS_PUSH:
        note = "A pushing terminal sends user changes by itself (USERINFO); no read can be requested. This answer is what it has pushed and the last agent read."
    else:
        note = "No agent carries this device, so nobody can read it. Rehome it to an office agent."
    return {"requested": requested_at is not None, "requested_at": requested_at, "transport": kind, "note": note}


def reconciliation(device: Device, refresh: dict | None = None) -> dict:
    return {"device": device, "transport": health.transport(device), "refresh": refresh, **roster.categorise(device), "reconciliation": roster.reconcile(device)}


def mapping_report() -> dict:
    """The device → office → agent chain for every terminal and where it contradicts itself (read-only)."""
    devices = list(devices_queryset()[: MAPPING_REPORT_LIMIT + 1])
    truncated = len(devices) > MAPPING_REPORT_LIMIT
    devices = devices[:MAPPING_REPORT_LIMIT]
    agents = list(Agent.objects.select_related("office", "credential").order_by("code"))
    offices = {}
    for device in devices:
        if device.office_id:
            entry = offices.setdefault(device.office_id, {"office": device.office, "devices": [], "agents": set()})
            entry["devices"].append(device.name)
            if device.agent_id:
                entry["agents"].add(device.agent.code)
    serving: dict[int, list[Device]] = {}
    for device in devices:
        if device.agent_id:
            serving.setdefault(device.agent_id, []).append(device)
    return {
        "devices": devices,
        "offices": [{"office": entry["office"], "devices": sorted(entry["devices"]), "agents": sorted(entry["agents"])} for entry in sorted(offices.values(), key=lambda item: item["office"].name)],
        "agents": [
            {
                "agent": agent,
                "status": health.agent_status(agent),
                "serves_devices": [device.name for device in serving.get(agent.pk, [])],
                "serves_offices": sorted({device.office.name for device in serving.get(agent.pk, []) if device.office_id}),
            }
            for agent in agents
        ],
        "inconsistencies": [device for device in devices if not health.mapping(device)["mapping_consistent"]],
        "truncated": truncated,
    }
