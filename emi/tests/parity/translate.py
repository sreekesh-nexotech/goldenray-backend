"""Legacy EMI requests/responses ↔ the canonical contract (what the ``/legacy/`` shim will do; used by the parity test).

The only contract change is identity: integer ids never leave the platform, so

* a request's size id (``size_id``, or its aliases ``installation_id`` / ``id``, read with the legacy
  ``_to_int(a or b or c)``) becomes ``size_uid`` through ``core_legacy_map``; an id that is not a number becomes a
  non-uid text (the same 400), an unknown id a uid no size has (the same answer as the legacy for an unknown id);
* a response's ``size_id`` / ``rule_id`` / ``id`` becomes ``size_uid`` / ``rule_uid`` / ``uid`` through the map;
* the legacy error text naming ``size_id`` names ``size_uid``.
"""

from __future__ import annotations

import json
import uuid
from decimal import Decimal

from core.models import LegacyMap
from emi.models import Bank, InterestRateRule, SystemSize

UNKNOWN_UID = str(uuid.UUID(int=0))
TABLES = {"emi_system_size": SystemSize, "emi_interest_rate_rule": InterestRateRule, "emi_bank": Bank}


def uid_maps() -> dict[str, dict[int, str]]:
    maps: dict[str, dict[int, str]] = {}
    for table, model in TABLES.items():
        targets = dict(LegacyMap.objects.filter(source_system="BACKEND", source_table=table).values_list("target_id", "source_id"))
        maps[table] = {int(targets[pk]): str(uid) for pk, uid in model.all_objects.filter(pk__in=targets).values_list("pk", "uid")}
    return maps


def request(body, maps):
    if not isinstance(body, dict):
        return body
    body = dict(body)
    legacy = body.get("size_id") or body.get("installation_id") or body.get("id")
    for key in ("size_id", "installation_id", "id"):
        body.pop(key, None)
    if legacy is None or legacy == "":
        return body
    try:
        legacy_id = int(legacy)
    except (TypeError, ValueError):
        body["size_uid"] = "not-a-uid"
        return body
    body["size_uid"] = maps["emi_system_size"].get(legacy_id, UNKNOWN_UID)
    return body


def response(value, maps, *, table: str | None = None):
    if isinstance(value, list):
        return [response(item, maps, table=table) for item in value]
    if not isinstance(value, dict):
        return value
    out = {}
    for key, item in value.items():
        if key == "size_id":
            out["size_uid"] = None if item is None else maps["emi_system_size"][item]
        elif key == "rule_id":
            out["rule_uid"] = None if item is None else maps["emi_interest_rate_rule"][item]
        elif key == "id" and table:
            out["uid"] = maps[table][item]
        else:
            out[key] = response(item, maps)
    return out


def message(text: str) -> str:
    return text.replace("size_id", "size_uid")


def loads(text: str):
    """Parse a recorded request keeping every number's text (``1e400`` must reach the server as ``1e400``)."""
    return json.loads(text, parse_float=Decimal)


def dumps(value) -> str:
    """JSON text of :func:`loads` output: numbers are written back as recorded."""
    if isinstance(value, dict):
        return "{" + ", ".join(f"{json.dumps(key, ensure_ascii=False)}: {dumps(item)}" for key, item in value.items()) + "}"
    if isinstance(value, list):
        return "[" + ", ".join(dumps(item) for item in value) + "]"
    if isinstance(value, Decimal):
        return str(value)
    return json.dumps(value, ensure_ascii=False)
