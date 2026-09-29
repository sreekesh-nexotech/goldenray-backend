"""What devices contributes to other screens: the hr registries, the dashboard, the weekly ops report.

Installed by ``DevicesConfig.ready()``:

* ``hr.registries.device_mappings`` — ``GET hr/employees/<uid>/device-mappings/``: the person's enrolment, device by
  device (``device_state``/``software_state`` per row) and their presence across terminals;
* ``hr.registries.device_reconciler`` — ``POST hr/employees/reconcile-devices/`` (:mod:`devices.services.reconcile`);
* ``hr.registries.employee_dependencies["devices"]`` — ``device_mappings`` (an employee linked on a terminal has history);
* ``hr.registries.office_dependencies["devices"]`` — ``devices`` and ``agents`` that block deleting an office;
* ``hr.registries.office_summary["devices"]`` — the office's connection (one word + the evidence), its agents and its
  terminals; only for callers who may view devices;
* dashboard counters for the ``devices`` module and the ``devices`` section of the weekly ops report.
"""

from __future__ import annotations

from datetime import datetime

from accounts.services.authz import can
from devices.models import AdmsRequest, AdmsUnknownDevice, Agent, Device, DeviceUser
from devices.services import health, roster
from devices.services.common import now


def _device_ref(device: Device) -> dict:
    return {"uid": device.uid, "name": device.name, "serial_number": device.serial_number or device.expected_serial, "display_label": health.display_label(device)}


def _agent_ref(agent: Agent | None, at=None) -> dict | None:
    if agent is None:
        return None
    return {"uid": agent.uid, "code": agent.code, "name": agent.name, "status": health.agent_status(agent, at)}


def employee_device_mappings(employee) -> dict:
    rows = list(roster.mapping_rows([employee.pk]))
    states = roster.describe(rows)
    presence = roster.employee_presence([employee.pk])[employee.pk]
    mappings = [
        {
            "device_user_uid": row.uid,
            "device": {**_device_ref(row.device), "office": row.device.office.name if row.device.office_id else None, "is_active": row.device.is_active},
            "pin": row.pin,
            "name": row.name,
            "last_seen_on_device_at": row.last_seen_at,
            **{key: states[row.pk][key] for key in ("device_state", "software_state", "sync_state", "device_confirmed_at", "is_active_user", "needs_device_removal")},
        }
        for row in rows
    ]
    return {"mappings": mappings, **presence, "device_deletion_supported": roster.DEVICE_DELETION_SUPPORTED}


def employee_dependencies(employee) -> dict:
    return {"device_mappings": DeviceUser.objects.filter(employee=employee).count()}


def office_dependencies(office) -> dict:
    return {"devices": Device.objects.filter(office=office).count(), "agents": Agent.objects.filter(office=office).count()}


def office_summary(office, day, user) -> dict | None:
    """The office's connection block (``None`` for callers who may not view devices)."""
    if not can(user, "devices", "view"):
        return None
    at = now()
    devices = list(Device.objects.filter(office=office, is_active=True).select_related("office", "agent__office", "agent__credential").order_by("name"))
    agents = list(Agent.objects.filter(office=office).select_related("office", "credential"))
    agent = health.office_agent(office.pk, devices, agents, at)
    connection = health.office_connection(agent, devices, at)
    connection["agent"] = _agent_ref(agent, at)
    return {
        "connection": connection,
        "agents": [_agent_ref(item, at) for item in health.office_agents(office.pk, devices, agents)],
        "devices": [
            {**_device_ref(device), **{key: value for key, value in health.device_block(device, at).items() if key in ("connection_state", "transport", "status", "seconds_since_contact")}}
            for device in devices
        ],
    }


def dashboard_counters(user) -> dict[str, int]:
    at = now()
    devices = list(Device.objects.filter(is_active=True).select_related("agent"))
    states = [health.connection_state(device, at) for device in devices]
    agents = list(Agent.objects.select_related("credential"))
    return {
        "devices": len(devices),
        "devices_online": states.count(health.ONLINE),
        "devices_degraded": states.count(health.DEGRADED),
        "devices_offline": states.count(health.OFFLINE),
        "devices_never_seen": states.count(health.NEVER_SEEN),
        "devices_identity_mismatch": states.count(health.IDENTITY_MISMATCH),
        "agents": len(agents),
        "agents_offline": sum(1 for agent in agents if health.agent_status(agent, at) == health.OFFLINE),
        "unlinked_device_users": DeviceUser.objects.filter(employee__isnull=True, device__is_active=True, device__deleted_at__isnull=True).count(),
        "adms_unknown_devices": AdmsUnknownDevice.objects.count(),
    }


def ops_section(since: datetime) -> dict:
    """Weekly ops report (PLAN §5.6): agent offline hours, ADMS unknown devices, identity mismatches."""
    at = now()
    agents = []
    for agent in Agent.objects.filter(is_active=True).select_related("credential").order_by("code"):
        status = health.agent_status(agent, at)
        silent = health.seconds_since_heartbeat(agent, at)
        agents.append(
            {"code": agent.code, "status": status, "last_heartbeat_at": agent.last_heartbeat_at, "offline_hours": round(silent / 3600, 1) if status == health.OFFLINE and silent is not None else 0}
        )
    return {
        "agents": agents,
        "adms_unknown_devices_seen": AdmsUnknownDevice.objects.filter(last_seen_at__gte=since).count(),
        "adms_requests": AdmsRequest.objects.filter(received_at__gte=since).count(),
        "devices_identity_mismatch": Device.objects.filter(is_active=True, identity_status=Device.IdentityStatus.IDENTITY_MISMATCH).count(),
    }


def install() -> None:
    from core import dashboard, ops_report
    from devices.services import reconcile
    from hr import registries

    registries.device_mappings.set(employee_device_mappings)
    registries.device_reconciler.set(reconcile.reconcile_employees)
    registries.employee_dependencies.register("devices")(employee_dependencies)
    registries.office_dependencies.register("devices")(office_dependencies)
    registries.office_summary.register("devices")(office_summary)
    dashboard.register("devices")(dashboard_counters)
    ops_report.register("devices")(ops_section)
