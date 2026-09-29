"""Old ``/api/calculate-solar*/`` and ``/api/emi-calculator*/`` (legacy ``goldenray`` calculator views).

The canonical services take the legacy bodies unchanged (``calculators.services.calculate.run``,
``emi.services.calculator``); the only translation is identity (docs/decisions/calculators-emi.md, hand-over): the
EMI size is posted as the legacy integer ``size_id`` (aliases ``installation_id`` / ``id``, read with the legacy
``_to_int(a or b or c)``) and answered as ``size_id`` / ``rule_id`` / ``id``; the platform's ``size_uid`` /
``rule_uid`` / ``uid`` are renamed back in place (key order kept). Errors are the legacy ``{"error": message}``.
"""

from __future__ import annotations

import uuid

from calculators.services.calculate import run
from emi.models import Bank, InterestRateRule, SystemSize
from emi.serializers.public import PublicEmiConfigSerializer
from emi.services import calculator
from legacy.services.ids import BACKEND, legacy_ids, resolve_pk

UNKNOWN_UID = str(uuid.UUID(int=0))
CALCULATORS = {"calculate-solar": "basic", "calculate-solar-new": "basic_v2", "calculate-solar-advanced": "advanced"}
TABLES = {SystemSize: "emi_system_size", InterestRateRule: "emi_interest_rate_rule", Bank: "emi_bank"}
CONFIG_ONLY_SETTINGS = ("disclaimer_en", "disclaimer_ml")


def solar(endpoint: str, body) -> dict:
    return run(CALCULATORS[endpoint], body)


def _uid_to_legacy(model, uids) -> dict[str, int]:
    uids = {str(uid) for uid in uids if uid}
    if not uids:
        return {}
    pks = dict(model.all_objects.filter(uid__in=uids).values_list("uid", "pk"))
    ids = legacy_ids(model, pks.values(), system=BACKEND, table=TABLES[model])
    return {str(uid): ids[pk] for uid, pk in pks.items()}


def _collect(value, key: str, found: set) -> set:
    if isinstance(value, dict):
        for item_key, item in value.items():
            if item_key == key and item:
                found.add(str(item))
            else:
                _collect(item, key, found)
    elif isinstance(value, list):
        for item in value:
            _collect(item, key, found)
    return found


def _rename(value, renames: dict[str, tuple[str, dict]]):
    """``{"size_uid": u}`` → ``{"size_id": legacy id}`` everywhere, keeping the key's position."""
    if isinstance(value, list):
        return [_rename(item, renames) for item in value]
    if not isinstance(value, dict):
        return value
    out = {}
    for key, item in value.items():
        if key in renames:
            name, ids = renames[key]
            out[name] = None if item is None else ids.get(str(item))
        else:
            out[key] = _rename(item, renames)
    return out


def _legacy_response(body):
    sizes = _uid_to_legacy(SystemSize, _collect(body, "size_uid", set()))
    rules = _uid_to_legacy(InterestRateRule, _collect(body, "rule_uid", set()))
    return _rename(body, {"size_uid": ("size_id", sizes), "rule_uid": ("rule_id", rules)})


def _size_uid(legacy) -> str:
    try:
        legacy_id = int(legacy)
    except (TypeError, ValueError):
        return "not-a-uid"  # the engine's "size not found / invalid" answer, as the legacy int() failure
    pk = resolve_pk(SystemSize, legacy_id, system=BACKEND, table=TABLES[SystemSize])
    uid = SystemSize.all_objects.filter(pk=pk).values_list("uid", flat=True).first() if pk else None
    return str(uid) if uid else UNKNOWN_UID


def _canonical_request(body):
    if not isinstance(body, dict):
        return body
    body = dict(body)
    legacy = body.get("size_id") or body.get("installation_id") or body.get("id")
    for key in ("size_id", "installation_id", "id"):
        body.pop(key, None)
    if legacy is not None and legacy != "":
        body["size_uid"] = _size_uid(legacy)
    return body


def message(text: str) -> str:
    return text.replace("size_uid", "size_id")


def emi_calculate(body) -> dict:
    return _legacy_response(calculator.calculate(_canonical_request(body)))


def emi_quotation(body) -> dict:
    return _legacy_response(calculator.quotation(_canonical_request(body)))


def emi_config() -> dict:
    data = PublicEmiConfigSerializer(calculator.public_config()).data
    settings = {key: value for key, value in data["settings"].items() if key not in CONFIG_ONLY_SETTINGS}
    sizes = _uid_to_legacy(SystemSize, [row["uid"] for row in data["system_sizes"]])
    banks = _uid_to_legacy(Bank, [row["uid"] for row in data["banks"]])
    return {
        "settings": settings,
        "system_sizes": [_rename(row, {"uid": ("id", sizes)}) for row in data["system_sizes"]],
        "banks": [_rename(row, {"uid": ("id", banks)}) for row in data["banks"]],
    }


def emi_config_namespaces() -> list[str]:
    return calculator.cache_namespaces()
