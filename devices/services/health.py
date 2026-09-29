"""The one place that decides whether a terminal, an agent or an office is connected (eSSL D4, without its defects).

Every screen (devices, agents, office summary, dashboard, mapping report) renders what this module decides and
computes nothing of its own. Nothing here contacts anything, and nothing is stored: there is no ``is_online`` column.
Every judgement is derived from timestamps something real reported:

* a **pushing** terminal (``adms_enabled``) is judged only on ``adms_last_seen_at`` — its office's agent is not
  consulted in either direction (a lapsed agent cannot drag it OFFLINE, a healthy one cannot vouch for it);
* an **agent-delivered** terminal is judged on ``last_seen_at``, which only moves when its agent actually reached it
  on the LAN (a heartbeat reporting ``reachable: true``, an announce, an upload) — the eSSL sticky ``is_online`` flag
  that stayed ONLINE forever after its agent died (spec §I.19) does not exist;
* both use the same thresholds (``DEVICES_ONLINE_SECONDS`` 300 / ``DEVICES_OFFLINE_SECONDS`` 900, the ADMS ones);
  an agent configured to report less often than that widens its devices' window to its own ``offline_after_seconds``;
* a mismatch outranks everything (the wrong hardware is worse news than silence), and NEVER_SEEN (an installation
  still to finish) stays distinct from OFFLINE (an outage).

Vocabularies — device ``connection_state``: ONLINE DEGRADED OFFLINE NEVER_SEEN IDENTITY_MISMATCH; ``adms_state`` adds
UNKNOWN (not a pushing terminal); ``status``: VERIFIED IDENTITY_MISMATCH UNVERIFIED OFFLINE; ``transport``: ADMS_PUSH
AGENT_DELIVERED UNASSIGNED (the eSSL SERVER_PULL transport is gone: the server never dials a terminal); agent
``status``: ONLINE DEGRADED OFFLINE REVOKED; office ``connection``: ONLINE DEGRADED OFFLINE AWAITING_DEVICE
IDENTITY_MISMATCH NO_AGENT REVOKED.
"""

from __future__ import annotations

from datetime import datetime

from devices.services.common import now as _now
from devices.services.common import offline_seconds, online_seconds

ONLINE = "ONLINE"
DEGRADED = "DEGRADED"
OFFLINE = "OFFLINE"
NEVER_SEEN = "NEVER_SEEN"
IDENTITY_MISMATCH = "IDENTITY_MISMATCH"
UNKNOWN = "UNKNOWN"
REVOKED = "REVOKED"
AWAITING_DEVICE = "AWAITING_DEVICE"
NO_AGENT = "NO_AGENT"
VERIFIED = "VERIFIED"
UNVERIFIED = "UNVERIFIED"

ADMS_PUSH = "ADMS_PUSH"
AGENT_DELIVERED = "AGENT_DELIVERED"
UNASSIGNED = "UNASSIGNED"

CONNECTION_STATES = (ONLINE, DEGRADED, OFFLINE, NEVER_SEEN, IDENTITY_MISMATCH)
ADMS_STATES = (*CONNECTION_STATES, UNKNOWN)
DEVICE_STATUSES = (VERIFIED, IDENTITY_MISMATCH, UNVERIFIED, OFFLINE)
TRANSPORTS = (ADMS_PUSH, AGENT_DELIVERED, UNASSIGNED)
AGENT_STATUSES = (ONLINE, DEGRADED, OFFLINE, REVOKED)
OFFICE_STATES = (ONLINE, DEGRADED, OFFLINE, AWAITING_DEVICE, IDENTITY_MISMATCH, NO_AGENT, REVOKED)

DEVICE_LABEL = {
    ONLINE: "Online",
    DEGRADED: "Degraded",
    OFFLINE: "Offline",
    NEVER_SEEN: "Never seen",
    IDENTITY_MISMATCH: "Wrong device",
    UNKNOWN: "Unknown",
}
DEVICE_NOTE = {
    ONLINE: "connected and reporting in",
    DEGRADED: "late reporting in",
    OFFLINE: "has stopped reporting in",
    NEVER_SEEN: "registered, never called in yet",
    IDENTITY_MISMATCH: "a different terminal answered at this address",
    UNKNOWN: "no transport established for this device",
}
OFFICE_LABEL = {
    ONLINE: "Online",
    DEGRADED: "Degraded",
    OFFLINE: "Offline",
    AWAITING_DEVICE: "Awaiting device",
    IDENTITY_MISMATCH: "Wrong device",
    NO_AGENT: "No agent",
    REVOKED: "Revoked",
}
TRANSPORT_LABEL = {
    ADMS_PUSH: "Device pushes to the platform",
    AGENT_DELIVERED: "Office agent uploads",
    UNASSIGNED: "No agent and no push configured",
}
_AGENT_RANK = {ONLINE: 0, DEGRADED: 1, OFFLINE: 2, REVOKED: 3}


def _age(moment: datetime | None, at: datetime | None) -> int | None:
    if moment is None:
        return None
    return max(0, int(((at or _now()) - moment).total_seconds()))


def _grade(age: int | None, online: int, offline: int) -> str:
    if age is None:
        return NEVER_SEEN
    if age <= online:
        return ONLINE
    if age <= offline:
        return DEGRADED
    return OFFLINE


# --------------------------------------------------------------------------------------------------------------------
# Agents
# --------------------------------------------------------------------------------------------------------------------
def agent_is_revoked(agent) -> bool:
    credential = agent.credential
    return not agent.is_active or credential is None or credential.revoked_at is not None


def agent_is_online(agent, at: datetime | None = None) -> bool:
    age = _age(agent.last_heartbeat_at, at)
    return age is not None and age <= agent.offline_after_seconds


def agent_status(agent, at: datetime | None = None) -> str:
    """REVOKED (credential withdrawn or agent disabled) · OFFLINE (no heartbeat inside its window) · DEGRADED
    (reporting, but an error, failing uploads or a queue above its threshold) · ONLINE."""
    if agent_is_revoked(agent):
        return REVOKED
    if not agent_is_online(agent, at):
        return OFFLINE
    if agent.last_error or agent.failed_uploads > 0 or agent.queued_records > agent.degraded_queue_threshold:
        return DEGRADED
    return ONLINE


def seconds_since_heartbeat(agent, at: datetime | None = None) -> int | None:
    return _age(agent.last_heartbeat_at, at)


# --------------------------------------------------------------------------------------------------------------------
# Devices
# --------------------------------------------------------------------------------------------------------------------
def transport(device) -> str:
    """How the terminal's punches arrive — configuration, not a guess. ``adms_enabled`` outranks an agent."""
    if device.adms_enabled:
        return ADMS_PUSH
    if device.agent_id is not None:
        return AGENT_DELIVERED
    return UNASSIGNED


def awaiting_discovery(device) -> bool:
    """Known hardware whose LAN address is not established yet (an agent finds it by serial with ``discover``)."""
    return not device.ip_address


def _windows(device) -> tuple[int, int]:
    online, offline = online_seconds(), offline_seconds()
    agent = device.agent if device.agent_id is not None else None
    if agent is not None and agent.offline_after_seconds > online:
        online = agent.offline_after_seconds
        offline = max(offline, online)
    return online, offline


def adms_state(device, at: datetime | None = None) -> str:
    if not device.adms_enabled:
        return UNKNOWN
    if device.identity_status == IDENTITY_MISMATCH:
        return IDENTITY_MISMATCH
    return _grade(_age(device.adms_last_seen_at, at), online_seconds(), offline_seconds())


def connection_state(device, at: datetime | None = None) -> str:
    if device.identity_status == IDENTITY_MISMATCH:
        return IDENTITY_MISMATCH
    if device.adms_enabled:
        return adms_state(device, at)
    online, offline = _windows(device)
    return _grade(_age(device.last_seen_at, at), online, offline)


def seconds_since_contact(device, at: datetime | None = None) -> int | None:
    return _age(device.adms_last_seen_at if device.adms_enabled else device.last_seen_at, at)


def device_status(device, at: datetime | None = None) -> str:
    """The identity word of the device list, derived from :func:`connection_state` so the two never disagree."""
    state = connection_state(device, at)
    if state == IDENTITY_MISMATCH:
        return IDENTITY_MISMATCH
    if state == NEVER_SEEN:
        return UNVERIFIED
    if not device.adms_enabled and device.identity_status != VERIFIED:
        return UNVERIFIED
    return VERIFIED if state == ONLINE else OFFLINE


def display_label(device) -> str:
    """The registered name is the name; the serial rides along — never instead of the name."""
    serial = device.serial_number or device.expected_serial
    return f"{device.name} · {serial}" if serial else device.name


def mapping(device) -> dict:
    """The device → office → agent chain. The device's office is where it stands; the agent's own office is a label,
    and a contradiction is reported (``mapping_consistent`` false) rather than absorbed."""
    agent = device.agent if device.agent_id is not None else None
    consistent = agent is None or agent.office_id == device.office_id
    note = ""
    if not consistent:
        agent_office = agent.office.name if agent.office_id else "no office"
        device_office = device.office.name if device.office_id else "no office"
        note = f"{agent.code} is filed under {agent_office}, but delivers this device in {device_office}."
    return {"mapping_consistent": consistent, "mapping_note": note}


def device_block(device, at: datetime | None = None) -> dict:
    """The connection state of one terminal, worded, for any screen that shows it."""
    state = connection_state(device, at)
    kind = transport(device)
    return {
        "status": device_status(device, at),
        "connection_state": state,
        "connection_label": DEVICE_LABEL.get(state, state),
        "connection_note": DEVICE_NOTE.get(state, ""),
        "transport": kind,
        "transport_label": TRANSPORT_LABEL[kind],
        "adms_state": adms_state(device, at),
        "seconds_since_contact": seconds_since_contact(device, at),
        "is_connected": state == ONLINE,
        "awaiting_discovery": awaiting_discovery(device),
        "display_label": display_label(device),
        **mapping(device),
    }


# --------------------------------------------------------------------------------------------------------------------
# Offices
# --------------------------------------------------------------------------------------------------------------------
def office_agents(office_id, devices: list, agents: list) -> list:
    """The agents that actually serve an office: the ones its devices are bound to (not the agents filed under it);
    an office without devices falls back to the agents filed there."""
    serving = {device.agent_id: device.agent for device in devices if device.agent_id is not None}
    if serving:
        return sorted(serving.values(), key=lambda agent: agent.code)
    return sorted((agent for agent in agents if agent.office_id == office_id), key=lambda agent: agent.code)


def office_agent(office_id, devices: list, agents: list, at: datetime | None = None):
    """The healthiest agent serving the office (ONLINE, then DEGRADED, OFFLINE, REVOKED)."""
    found = office_agents(office_id, devices, agents)
    if not found:
        return None
    return sorted(found, key=lambda agent: (_AGENT_RANK.get(agent_status(agent, at), 9), agent.code))[0]


def office_connection(agent, devices: list, at: datetime | None = None) -> dict:
    """One word for whether an office is reachable, and the evidence behind it (eSSL ``office_connection``)."""
    mismatched = [device for device in devices if device.identity_status == IDENTITY_MISMATCH]
    pushing = [device for device in devices if device.adms_enabled]
    if pushing and not mismatched:
        states = {connection_state(device, at) for device in pushing}
        if ONLINE in states:
            state = ONLINE
        elif DEGRADED in states:
            state = DEGRADED
        elif states == {NEVER_SEEN}:
            state = AWAITING_DEVICE
        else:
            state = OFFLINE
        route = ADMS_PUSH
    else:
        route = "AGENT"
        status = agent_status(agent, at) if agent is not None else None
        if agent is None:
            state = NO_AGENT
        elif status == REVOKED:
            state = REVOKED
        elif mismatched:
            state = IDENTITY_MISMATCH
        elif status == OFFLINE:
            state = OFFLINE
        elif not devices or all(awaiting_discovery(device) for device in devices):
            state = AWAITING_DEVICE
        elif status == DEGRADED:
            state = DEGRADED
        elif any(connection_state(device, at) == ONLINE and device.identity_status == VERIFIED for device in devices):
            state = ONLINE
        else:
            state = DEGRADED
    last_sync = max((device.last_sync_at for device in devices if device.last_sync_at), default=None)
    last_adms = max((device.adms_last_seen_at for device in devices if device.adms_last_seen_at), default=None)
    return {
        "status": state,
        "status_label": OFFICE_LABEL.get(state, state),
        "transport": route,
        "agent": agent,
        "agent_status": agent_status(agent, at) if agent is not None else None,
        "last_heartbeat_at": agent.last_heartbeat_at if agent is not None else None,
        "seconds_since_heartbeat": seconds_since_heartbeat(agent, at) if agent is not None else None,
        "queued_records": agent.queued_records if agent is not None else 0,
        "failed_uploads": agent.failed_uploads if agent is not None else 0,
        "last_error": (agent.last_error if agent is not None else "") or next((device.identity_message for device in mismatched), ""),
        "last_sync_at": last_sync,
        "last_adms_contact_at": last_adms,
        "devices_total": len(devices),
        "devices_online": sum(1 for device in devices if connection_state(device, at) == ONLINE),
        "devices_verified": sum(1 for device in devices if device.identity_status == VERIFIED),
        "devices_awaiting_discovery": sum(1 for device in devices if awaiting_discovery(device)),
        "devices_mismatched": len(mismatched),
        "devices_adms": len(pushing),
        "devices_adms_online": sum(1 for device in pushing if connection_state(device, at) == ONLINE),
        # contacted the receiver quoting one of these serials without being declared: shown, never acted on
        "devices_adms_undeclared": sum(1 for device in devices if not device.adms_enabled and device.adms_last_seen_at),
        "device_users": sum(device.user_count for device in devices),
    }
