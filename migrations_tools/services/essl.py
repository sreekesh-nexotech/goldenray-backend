"""The eSSL (PostgreSQL) import plan — PLAN §7.5 "eSSL", in FK order: users → HR masters → devices → punches, then the
v4 recompute and the v3/v4 per-employee-month status diff for HR (business default B-7, PLAN §7.6 #11).

Two results do not fit the batch counts and are handed to the command through ``Context.options["artefacts"]``: the
**agent credentials** (one new service token per active agent, shown once — the command writes them to a 0600 file)
and the **attendance diff report** (the command writes it to a file for HR).
"""

from __future__ import annotations

from datetime import timedelta

from django.utils import timezone
from django.utils.dateparse import parse_datetime

from attendance.services import legacy_import as attendance_import
from core.models import LegacyMap
from devices.services import legacy_import as devices_import
from hr.services import legacy_import as hr_import
from migrations_tools.services.runner import Context, Plan, Step

ESSL = LegacyMap.SourceSystem.ESSL
HR_TABLES = ("shifts", "offices", "employees", "holidays", "leave_records", "attendance_rules")
DEVICE_TABLES = ("agents", "devices", "device_users", "sync_logs", "protocol_mappings", "adms_unknown_devices", "adms_requests")


def artefacts(ctx: Context) -> dict:
    return ctx.options.setdefault("artefacts", {})


def _users(rows, ctx: Context) -> dict:
    """eSSL logins → accounts (ADMIN→Admin, HR→HR, USER/VIEWER→Staff; bcrypt hashes kept as ``bcrypt$…``)."""
    return {"users": hr_import.import_users(rows["users"], roles=rows["roles"], user=ctx.user)}


def _hr(rows, ctx: Context) -> dict:
    """Shifts, offices, employees, holidays, leave types (from the distinct ``leave_type`` strings), leave, rules — 1:1."""
    return {
        "shifts": hr_import.import_shifts(rows["shifts"], user=ctx.user),
        "offices": hr_import.import_offices(rows["offices"], user=ctx.user),
        "employees": hr_import.import_employees(rows["employees"], user=ctx.user),
        "holidays": hr_import.import_holidays(rows["holidays"], user=ctx.user),
        "leave_types": hr_import.import_leave_types(rows["leave_records"], user=ctx.user),
        "leave_records": hr_import.import_leave_records(rows["leave_records"], user=ctx.user),
        "attendance_rules": hr_import.import_attendance_rules(rows["attendance_rules"], user=ctx.user),
    }


def _devices(rows, ctx: Context) -> dict:
    """Agents (new credentials), devices (ADMS off until re-pointed), per-device PIN links (multi-device PINs listed),
    watermarks, protocol mappings, the ADMS quarantine list and the last 30 days of ADMS evidence."""
    results = devices_import.import_all({table: rows[table] for table in DEVICE_TABLES}, user=ctx.user)
    credentials = results["agents"].get("credentials") or []
    if credentials:
        artefacts(ctx).setdefault("agent_credentials", []).extend(credentials)
    return results


def _attendance(rows, ctx: Context) -> dict:
    """``attendance_raw`` → raw punches with the platform's content keys (duplicates across transports collapse, listed
    ``collapsed``); the v3 ``attendance`` days are not imported: v4 recomputes them and the diff goes to HR."""
    results = attendance_import.import_all({"attendance_raw": rows["attendance_raw"], "attendance": rows["attendance"]}, user=ctx.user)
    raw, days = results["attendance_raw"], results["attendance"]
    collapsed = raw.get("collapsed") or []
    raw["violations"] = [
        *raw["violations"],
        *(
            {
                "source_table": "attendance_raw",
                "source_id": item["source_id"],
                "code": "collapsed_duplicate",
                "severity": "info",
                "message": f"PIN {item['pin']} at {item['device_time']}: same punch as {item['kept']} ({item['reason']}).",
            }
            for item in collapsed
        ),
    ]
    artefacts(ctx)["attendance_diff"] = {
        "days_compared": days.get("days_compared", 0),
        "days_differing": days.get("days_differing", 0),
        "month_totals": attendance_import.month_totals(days),
        "months": days.get("months", []),
        "raw_punches": {"source_rows": len(rows["attendance_raw"]), "created": raw["created"], "collapsed": len(collapsed)},
    }
    days_result = {key: days[key] for key in ("created", "updated", "skipped", "violations")}
    return {"attendance_raw": raw, "attendance (v3 → v4 diff)": days_result}


def _recent_requests(rows):
    """The ADMS evidence the import keeps (younger than the retention window); older requests are not migrated."""
    from devices.services import adms_evidence

    cutoff = timezone.now() - timedelta(days=adms_evidence.retention_days())
    # evidence imported while it was inside the window stays expected (verify may run after it aged out)
    mapped = set(LegacyMap.objects.filter(source_system=ESSL, source_table="adms_requests").values_list("source_id", flat=True))
    keys = []
    for row in rows:
        received = row.get("received_at")
        received = parse_datetime(received) if isinstance(received, str) else received
        if str(row["id"]) in mapped or (received is not None and received >= cutoff):
            keys.append(("adms_requests", str(row["id"])))
    return keys


PLAN = Plan(
    source_system=ESSL,
    steps=(
        Step("essl.users", "users", ("users", "roles"), _users, "logins → accounts (bcrypt kept, upgraded to Argon2 at the next login)"),
        Step("essl.hr", "masters", HR_TABLES, _hr, "offices, shifts, employees, holidays, leave types and records, rules"),
        Step("essl.devices", "masters", DEVICE_TABLES, _devices, "agents (new credentials), devices, PIN links, watermarks, mappings, ADMS evidence"),
        Step("essl.attendance", "transactional", ("attendance_raw", "attendance"), _attendance, "raw punches (content dedup), v4 recompute, v3/v4 diff for HR"),
    ),
    not_migrated={"alembic_version": "framework table"},
    row_keys={
        "roles": lambda rows: [],  # mapped onto the seeded roles, not imported
        "sync_logs": lambda rows: [],  # only each device's latest USERS log is kept (a watermark); the rest is history
        "adms_requests": _recent_requests,
        "attendance": lambda rows: [],  # recomputed by engine v4 and compared (#11), never imported
    },
)
