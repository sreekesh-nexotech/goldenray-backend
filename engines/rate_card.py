"""Commercial configuration history and the Project Head rate card.

Ports of Flarize ``src/lib/commercialHistory.js`` (append-only versioned records: a change appends a version and marks
the previous ACTIVE one SUPERSEDED) and ``src/lib/projectRateCard.js`` (installation, vehicle, site-survey,
engineering-design, service and special-work rates, resolved EXACTLY — never a nearest or first-available rate).
The cost engine (:mod:`engines.cost`) resolves rates through :func:`resolve_rate`. Stores are plain dicts; every
function returns a new store.
"""

from __future__ import annotations

import copy
from typing import Any

from engines._jscompat import JsError, clean, is_nullish, js_number, js_str, js_str_key, jsget, json_equal, nullish, truthy

CONFIG_STATUS = {"ACTIVE": "ACTIVE", "SUPERSEDED": "SUPERSEDED", "ARCHIVED": "ARCHIVED"}
CONFIG_TYPE = {
    name: name
    for name in (
        "PROCUREMENT_PRICE",
        "INSTALLATION_RATE",
        "TRANSPORT_VEHICLE_RATE",
        "SITE_SURVEY_RATE",
        "ENGINEERING_DESIGN_RATE",
        "SERVICE_RATE",
        "SPECIAL_WORK_RATE",
        "MARGIN",
        "MISCELLANEOUS_PCT",
        "GST_CONFIG",
    )
}
CONFIG_OWNER = {
    "PROCUREMENT_PRICE": "PROCUREMENT",
    "INSTALLATION_RATE": "PROJECT_HEAD",
    "TRANSPORT_VEHICLE_RATE": "PROJECT_HEAD",
    "SITE_SURVEY_RATE": "PROJECT_HEAD",
    "ENGINEERING_DESIGN_RATE": "PROJECT_HEAD",
    "SERVICE_RATE": "PROJECT_HEAD",
    "SPECIAL_WORK_RATE": "PROJECT_HEAD",
    "MARGIN": "ADMIN",
    "MISCELLANEOUS_PCT": "ADMIN",
    "GST_CONFIG": "ADMIN",
}
HISTORY_ERROR = {"MISSING_AUDIT_FIELDS": "MISSING_AUDIT_FIELDS", "UNKNOWN_CONFIG_TYPE": "UNKNOWN_CONFIG_TYPE", "DUPLICATE_VERSION_ID": "DUPLICATE_VERSION_ID"}
REQUIRED_AUDIT_FIELDS = ("versionId", "changedBy", "changedAt", "changeReason", "effectiveFrom")

RATE_CARD_VERSION_FIELD = "projectRateCardVersion"
RATE_TYPE = {
    "INSTALLATION": "INSTALLATION_RATE",
    "TRANSPORT_VEHICLE": "TRANSPORT_VEHICLE_RATE",
    "SITE_SURVEY": "SITE_SURVEY_RATE",
    "ENGINEERING_DESIGN": "ENGINEERING_DESIGN_RATE",
    "SERVICE": "SERVICE_RATE",
    "SPECIAL_WORK": "SPECIAL_WORK_RATE",
}
RATE_UNIT = {"PER_PROJECT": "PER_PROJECT", "PER_KW": "PER_KW", "PER_KM": "PER_KM", "PER_VISIT": "PER_VISIT"}
RATE_ERROR = {"RATE_NOT_CONFIGURED": "RATE_NOT_CONFIGURED", "RATE_UNIT_INVALID": "RATE_UNIT_INVALID"}


class CommercialHistoryError(JsError):
    """``appendVersion`` refusals. The JS threw a plain ``Error`` whose message starts with the code."""

    js_name = "Error"


def _key(config_type: Any, record_id: Any) -> str:
    return f"{js_str(config_type)}::{js_str(record_id)}"


def _records(store: Any) -> dict:
    records = jsget(store, "records")
    return records if isinstance(records, dict) else {}


# ---------------------------------------------------------------------------------------------------------------
# commercialHistory.js
# ---------------------------------------------------------------------------------------------------------------


def create_history_store() -> dict:
    return {"records": {}}


def append_version(store: dict, entry: dict) -> dict:
    """Append a version; the previous ACTIVE one is copied as SUPERSEDED (``effectiveTo`` = the new ``effectiveFrom``)."""
    config_type = entry.get("configType")
    record_id = entry.get("recordId")
    if not isinstance(config_type, str) or config_type not in CONFIG_TYPE:
        code = HISTORY_ERROR["UNKNOWN_CONFIG_TYPE"]
        raise CommercialHistoryError(f'{code}: "{js_str(jsget(entry, "configType"))}"', code)
    missing = [f for f in REQUIRED_AUDIT_FIELDS if is_nullish(jsget(entry, f)) or entry.get(f) == ""]
    if missing:
        code = HISTORY_ERROR["MISSING_AUDIT_FIELDS"]
        raise CommercialHistoryError(f"{code}: {', '.join(missing)}. Every commercial change must record who, when, why and from when.", code)
    key = _key(config_type, record_id)
    existing = _records(store).get(key) or []
    if any(v.get("versionId") == entry["versionId"] for v in existing):
        code = HISTORY_ERROR["DUPLICATE_VERSION_ID"]
        raise CommercialHistoryError(f'{code}: "{js_str(entry["versionId"])}"', code)
    previous = next((v for v in existing if v.get("status") == CONFIG_STATUS["ACTIVE"]), None)
    superseded = [
        (
            {
                **v,
                "status": CONFIG_STATUS["SUPERSEDED"],
                "effectiveTo": nullish(jsget(v, "effectiveTo"), entry["effectiveFrom"]),
                "supersededBy": entry["versionId"],
            }
            if v.get("status") == CONFIG_STATUS["ACTIVE"]
            else v
        )
        for v in existing
    ]
    new_version = clean(
        {
            "configType": config_type,
            "recordId": jsget(entry, "recordId"),
            "owner": CONFIG_OWNER[config_type],
            "versionId": entry["versionId"],
            "value": copy.deepcopy(jsget(entry, "value")),
            "status": CONFIG_STATUS["ACTIVE"],
            "effectiveFrom": entry["effectiveFrom"],
            "effectiveTo": nullish(jsget(entry, "effectiveTo"), None),
            "changedBy": entry["changedBy"],
            "changedAt": entry["changedAt"],
            "changeReason": entry["changeReason"],
            "previousVersionId": nullish(jsget(previous, "versionId"), None),
            "supersededBy": None,
        }
    )
    return {**store, "records": {**_records(store), key: superseded + [new_version]}}


def archive_version(store: dict, *, config_type: Any, record_id: Any, version_id: Any, changed_by: Any, changed_at: Any, change_reason: Any) -> dict:
    """Mark one version ARCHIVED (still append-only: nothing is removed)."""
    key = _key(config_type, record_id)
    existing = _records(store).get(key) or []
    archived = [
        (clean({**v, "status": CONFIG_STATUS["ARCHIVED"], "archivedBy": changed_by, "archivedAt": changed_at, "archiveReason": change_reason}) if v.get("versionId") == version_id else v)
        for v in existing
    ]
    return {**store, "records": {**_records(store), key: archived}}


def get_history(store: Any, config_type: Any, record_id: Any) -> list[dict]:
    """Every version, oldest first (by ``changedAt``, then ``versionId``)."""
    versions = list(_records(store).get(_key(config_type, record_id)) or [])
    return sorted(versions, key=lambda v: (js_str_key(js_str(jsget(v, "changedAt"))), js_str_key(js_str(jsget(v, "versionId")))))


def get_current_version(store: Any, config_type: Any, record_id: Any) -> dict | None:
    return next((v for v in _records(store).get(_key(config_type, record_id)) or [] if v.get("status") == CONFIG_STATUS["ACTIVE"]), None)


def get_version(store: Any, config_type: Any, record_id: Any, version_id: Any) -> dict | None:
    return next((v for v in _records(store).get(_key(config_type, record_id)) or [] if v.get("versionId") == version_id), None)


def get_version_effective_at(store: Any, config_type: Any, record_id: Any, date: Any) -> dict | None:
    """The version applicable at ``date``: latest ``effectiveFrom <= date`` with ``effectiveTo`` null or later; never ARCHIVED."""
    when = js_str_key(js_str(date))
    applicable = [
        v
        for v in _records(store).get(_key(config_type, record_id)) or []
        if v.get("status") != CONFIG_STATUS["ARCHIVED"]
        and js_str_key(js_str(jsget(v, "effectiveFrom"))) <= when
        and (is_nullish(jsget(v, "effectiveTo")) or js_str_key(js_str(v["effectiveTo"])) > when)
    ]
    applicable.sort(key=lambda v: js_str_key(js_str(jsget(v, "effectiveFrom"))))
    return applicable[-1] if applicable else None


def compare_versions(old: Any, new: Any) -> dict | None:
    """Field-level differences between two versions' values, with the new version's audit fields."""
    if not truthy(old) or not truthy(new):
        return None
    old_value = old.get("value") if isinstance(old.get("value"), dict) else {}
    new_value = new.get("value") if isinstance(new.get("value"), dict) else {}
    fields = sorted(set(old_value) | set(new_value), key=js_str_key)
    changes = [{"field": f, "oldValue": nullish(jsget(old_value, f), None), "newValue": nullish(jsget(new_value, f), None)} for f in fields if not json_equal(jsget(old_value, f), jsget(new_value, f))]
    return clean(
        {
            "configType": jsget(new, "configType"),
            "recordId": jsget(new, "recordId"),
            "from": clean({"versionId": jsget(old, "versionId"), "effectiveFrom": jsget(old, "effectiveFrom"), "status": jsget(old, "status")}),
            "to": clean({"versionId": jsget(new, "versionId"), "effectiveFrom": jsget(new, "effectiveFrom"), "status": jsget(new, "status")}),
            "changedBy": jsget(new, "changedBy"),
            "changedAt": jsget(new, "changedAt"),
            "changeReason": jsget(new, "changeReason"),
            "changes": changes,
        }
    )


def history_view(store: Any, config_type: Any, record_id: Any) -> dict:
    """CURRENT + HISTORY for a configuration record."""
    history = get_history(store, config_type, record_id)
    current = get_current_version(store, config_type, record_id)
    fields = ("versionId", "value", "effectiveFrom", "changedBy", "changedAt", "changeReason", "status")
    history_fields = ("versionId", "value", "effectiveFrom", "effectiveTo", "changedBy", "changedAt", "changeReason", "status", "previousVersionId", "supersededBy")
    return clean(
        {
            "configType": config_type,
            "recordId": record_id,
            "owner": jsget(CONFIG_OWNER, config_type),
            "current": clean({f: jsget(current, f) for f in fields}) if current is not None else None,
            "history": [clean({f: jsget(v, f) for f in history_fields}) for v in history],
        }
    )


# ---------------------------------------------------------------------------------------------------------------
# projectRateCard.js
# ---------------------------------------------------------------------------------------------------------------


def create_rate_card(*, card_version: Any = None) -> dict:
    return {"cardVersion": card_version, "store": create_history_store()}


def set_rate(
    card: dict,
    *,
    rate_type: Any,
    record_id: Any,
    value: dict,
    rate_id: Any = None,
    version_id: Any = None,
    changed_by: Any = None,
    changed_at: Any = None,
    change_reason: Any = None,
    effective_from: Any = None,
    effective_to: Any = None,
    card_version: Any = None,
) -> dict:
    """Add or change a rate (append-only; the previous version is superseded, never edited)."""
    store = append_version(
        card["store"],
        clean(
            {
                "configType": rate_type,
                "recordId": record_id,
                "value": {"rateId": nullish(rate_id, record_id), **(value or {})},
                "versionId": version_id,
                "changedBy": changed_by,
                "changedAt": changed_at,
                "changeReason": change_reason,
                "effectiveFrom": effective_from,
                "effectiveTo": effective_to,
            }
        ),
    )
    return {"cardVersion": nullish(card_version, jsget(card, "cardVersion")), "store": store}


def resolve_rate(card: Any, *, rate_type: Any, record_id: Any, at: Any = None) -> dict:
    """``{ok, value, versionId, effectiveFrom, changedBy, status, reason}`` or ``{ok: False, code, reason}``."""
    if not truthy(jsget(card, "store")):
        return {"ok": False, "code": RATE_ERROR["RATE_NOT_CONFIGURED"], "reason": "No rate card supplied."}
    version = get_version_effective_at(card["store"], rate_type, record_id, at) if truthy(at) else get_current_version(card["store"], rate_type, record_id)
    if version is None or version.get("status") == CONFIG_STATUS["ARCHIVED"]:
        suffix = f" effective at {js_str(at)}." if truthy(at) else "."
        return {
            "ok": False,
            "code": RATE_ERROR["RATE_NOT_CONFIGURED"],
            "reason": f'No {js_str(rate_type)} rate configured for "{js_str(record_id)}"{suffix} No nearest, first-available or legacy rate is substituted.',
        }
    return clean(
        {
            "ok": True,
            "value": jsget(version, "value"),
            "versionId": jsget(version, "versionId"),
            "effectiveFrom": jsget(version, "effectiveFrom"),
            "changedBy": jsget(version, "changedBy"),
            "status": jsget(version, "status"),
            "reason": None,
        }
    )


def installation_key(system_size_kw: Any, installation_type: Any) -> str:
    """Installation rates are keyed by size + type (``"3kW/FLAT"``). EXACT match only."""
    return f"{js_str(system_size_kw)}kW/{js_str(installation_type)}"


def engineering_key(size_kw: Any, size_limit_kw: Any) -> str:
    return f"<={js_str(size_limit_kw)}kW" if js_number(size_kw) <= js_number(size_limit_kw) else f">{js_str(size_limit_kw)}kW"


def list_rates(card: Any) -> list[dict]:
    """Every rate on the card (for a Project Head review screen), keys in code-unit order."""
    records = _records(jsget(card, "store"))
    out = []
    for key in sorted(records, key=js_str_key):
        parts = key.split("::")
        config_type = parts[0]
        record_id = parts[1] if len(parts) > 1 else None
        versions = get_history(card["store"], config_type, record_id)
        current = next((v for v in versions if v.get("status") == CONFIG_STATUS["ACTIVE"]), None)
        out.append({"rateType": config_type, "recordId": record_id, "current": current, "versionCount": len(versions)})
    return out


def seed_rate_card_from_config(config: Any, *, changed_by: Any, changed_at: Any, change_reason: Any, effective_from: Any, card_version: Any) -> dict:
    """A rate card seeded from a plain cost configuration; every seeded record carries a full audit entry."""
    card = create_rate_card(card_version=card_version)
    audit = {"changed_by": changed_by, "changed_at": changed_at, "change_reason": change_reason, "effective_from": effective_from}
    for rate in jsget(jsget(config, "installation"), "rates") or []:
        key = installation_key(rate.get("systemSizeKw"), rate.get("installationType"))
        version_id = f"{js_str(rate['version'])}/{key}" if truthy(rate.get("version")) else f"INST-{js_str(rate.get('systemSizeKw'))}-{js_str(rate.get('installationType'))}"
        value = clean(
            {
                "systemSizeKw": jsget(rate, "systemSizeKw"),
                "installationType": jsget(rate, "installationType"),
                "unit": jsget(rate, "unit"),
                "rate": jsget(rate, "rate"),
            }
        )
        card = set_rate(card, rate_type=RATE_TYPE["INSTALLATION"], record_id=key, value=value, version_id=version_id, **audit)
    for vehicle in jsget(jsget(config, "transportation"), "vehicles") or []:
        value = clean({"vehicleType": jsget(vehicle, "vehicleType"), "ratePerKm": jsget(vehicle, "ratePerKm"), "unit": RATE_UNIT["PER_KM"]})
        version_id = vehicle.get("versionId") if truthy(vehicle.get("versionId")) else f"VEH-{js_str(jsget(vehicle, 'vehicleType'))}"
        card = set_rate(card, rate_type=RATE_TYPE["TRANSPORT_VEHICLE"], record_id=jsget(vehicle, "vehicleType"), value=value, version_id=version_id, **audit)
    survey = jsget(config, "siteSurvey")
    if truthy(survey) and not is_nullish(jsget(survey, "costPerProject")):
        value = clean(
            {
                "costPerProject": survey["costPerProject"],
                "unit": RATE_UNIT["PER_PROJECT"],
                "coverageKm": jsget(survey, "coverageKm"),
                "excessRatePerKm": nullish(jsget(survey, "excessRatePerKm"), None),
            }
        )
        card = set_rate(card, rate_type=RATE_TYPE["SITE_SURVEY"], record_id="STANDARD", value=value, version_id=survey.get("version") if truthy(survey.get("version")) else "SURVEY-1", **audit)
    design = jsget(config, "engineeringDesign")
    if truthy(design) and not is_nullish(jsget(design, "costPerProject")):
        value = clean({"costPerProject": design["costPerProject"], "unit": RATE_UNIT["PER_PROJECT"], "sizeLimitKw": jsget(design, "sizeLimitKw")})
        card = set_rate(
            card,
            rate_type=RATE_TYPE["ENGINEERING_DESIGN"],
            record_id=f"<={js_str(jsget(design, 'sizeLimitKw'))}kW",
            value=value,
            version_id=design.get("version") if truthy(design.get("version")) else "ENGDES-1",
            **audit,
        )
    service = jsget(config, "serviceAmc")
    if truthy(service) and not is_nullish(jsget(service, "costPerVisit")):
        value = clean(
            {
                "costPerVisit": service["costPerVisit"],
                "visitsPerYear": jsget(service, "visitsPerYear"),
                "serviceYears": jsget(service, "serviceYears"),
                "sizeLimitKw": jsget(service, "sizeLimitKw"),
                "unit": RATE_UNIT["PER_VISIT"],
            }
        )
        card = set_rate(card, rate_type=RATE_TYPE["SERVICE"], record_id="STANDARD", value=value, version_id=service.get("version") if truthy(service.get("version")) else "SERVICE-1", **audit)
    for work in jsget(jsget(config, "specialProjectWorks"), "rates") or []:
        value = clean({"workType": jsget(work, "workType"), "rate": jsget(work, "rate"), "unit": jsget(work, "unit")})
        version_id = work.get("versionId") if truthy(work.get("versionId")) else f"WORK-{js_str(jsget(work, 'workType'))}"
        card = set_rate(card, rate_type=RATE_TYPE["SPECIAL_WORK"], record_id=jsget(work, "workType"), value=value, version_id=version_id, **audit)
    return card


__all__ = [
    "CONFIG_OWNER",
    "CONFIG_STATUS",
    "CONFIG_TYPE",
    "HISTORY_ERROR",
    "RATE_ERROR",
    "RATE_TYPE",
    "RATE_UNIT",
    "CommercialHistoryError",
    "append_version",
    "archive_version",
    "compare_versions",
    "create_history_store",
    "create_rate_card",
    "engineering_key",
    "get_current_version",
    "get_history",
    "get_version",
    "get_version_effective_at",
    "history_view",
    "installation_key",
    "list_rates",
    "resolve_rate",
    "seed_rate_card_from_config",
    "set_rate",
]
