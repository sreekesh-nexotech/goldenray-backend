"""Replay the Flarize golden cases (``engines/tests/golden/commercial_*.json``) against the Python ports.

Every case records a call of the real JavaScript (``generate_commercial.mjs``) and what it returned or threw. The
adapters below make the same call on the Python engine; :func:`assert_same` then demands exact JSON equality
(numbers by value, booleans only equal booleans, ``NaN`` as ``null`` like ``JSON.stringify``).
"""

from __future__ import annotations

import copy
import functools
import inspect
import json
import math
import re
from decimal import Decimal
from pathlib import Path
from typing import Any, Callable

from engines import battery_compat as bc
from engines import bom_builder as bb
from engines import cost as ce
from engines import device_allocation as da
from engines import offers as of
from engines import pack_config as pc
from engines import pack_pricing as pp
from engines import package_registry as pr
from engines import pricing as pe
from engines import rate_card as rc
from engines._jscompat import UNDEFINED, JsError, truthy
from engines._money_compat import is_money, mul_exact, round_money, sum_exact

GOLDEN_DIR = Path(__file__).parent / "golden"
MODULES = ("money", "device_allocation", "bom", "pack_pricing", "pack_config", "package_registry", "cost", "landed", "pricing", "offers", "battery", "rate_card")


@functools.lru_cache(maxsize=None)
def load_golden(module: str) -> dict:
    with open(GOLDEN_DIR / f"commercial_{module}.json", encoding="utf-8") as handle:
        return json.load(handle)


@functools.lru_cache(maxsize=None)
def load_fixtures() -> dict:
    with open(GOLDEN_DIR / "fixtures" / "flarize_commercial.json", encoding="utf-8") as handle:
        return json.load(handle)


@functools.lru_cache(maxsize=None)
def bom_results() -> dict:
    return {case["id"]: case["result"] for case in load_golden("bom")["cases"] if case["fn"] == "buildBom" and "result" in case}


def fixed_now(module: str) -> str:
    return load_golden(module)["fixedNow"]


# ---------------------------------------------------------------------------------------------------------------
# Argument placeholders
# ---------------------------------------------------------------------------------------------------------------

_JS_VALUES = {"undefined": UNDEFINED, "NaN": math.nan, "Infinity": math.inf, "-Infinity": -math.inf}


def resolve_ref(name: str, local: dict) -> Any:
    head, *rest = name.split(".")
    fixtures = load_fixtures()
    synth = load_golden("bom").get("fixtures", {})
    value = local[head] if head in local else fixtures[head] if head in fixtures else synth[head]
    for key in rest:
        value = value[key]
    return copy.deepcopy(value)


def resolve(value: Any, local: dict) -> Any:
    if isinstance(value, list):
        return [resolve(v, local) for v in value]
    if isinstance(value, dict):
        if "$ref" in value:
            return resolve_ref(value["$ref"], local)
        if "$js" in value:
            return _JS_VALUES[value["$js"]]
        if "$bomLines" in value:
            return copy.deepcopy(bom_results()[value["$bomLines"]]["lines"])
        return {k: resolve(v, local) for k, v in value.items()}
    return value


# ---------------------------------------------------------------------------------------------------------------
# Calling conventions
# ---------------------------------------------------------------------------------------------------------------


def _snake(name: str) -> str:
    return re.sub(r"(?<!^)(?=[A-Z])", "_", name).lower()


def call_with_object(func: Callable, obj: Any, *positional: Any, **extra: Any) -> Any:
    """Call ``func(*positional, **kwargs)`` mapping a JS argument object onto its keyword-only parameters.

    A key the object lacks keeps the Python default, or is ``UNDEFINED`` when the parameter is required — exactly what
    the JS destructuring saw.
    """
    values = {_snake(k): v for k, v in obj.items()} if isinstance(obj, dict) else {}
    kwargs = {}
    for name, param in inspect.signature(func).parameters.items():
        if param.kind is not inspect.Parameter.KEYWORD_ONLY or name in extra:
            continue
        if name in values:
            kwargs[name] = values[name]
        elif param.default is inspect.Parameter.empty:
            kwargs[name] = UNDEFINED
    kwargs.update(extra)
    return func(*positional, **kwargs)


def _arg(args: list, index: int) -> Any:
    return args[index] if index < len(args) else UNDEFINED


def _pack_config_provider(spec: Any, local: dict) -> Any:
    if spec == "store":
        store = load_fixtures()["packStore"]
        return lambda source: copy.deepcopy(store["draft"]["config"] if source == "draft" else store["approved"]["config"])
    if spec is None:
        return None
    return resolve(spec, local)


def _bom_env(case: dict, local: dict) -> dict:
    env = case["env"]
    return {
        "catalog": resolve(env["catalog"], local),
        "registry": None if env["registry"] is None else resolve(env["registry"], local),
        "pack_config": _pack_config_provider(env["packConfig"], local),
    }


def _pack_config_scenario(args: list, now: str) -> list:
    store, steps = args
    outputs = []
    for step in steps:
        op = step["op"]
        try:
            if op == "describeStore":
                result = pc.describe_store(store)
            else:
                result = call_with_object(PACK_CONFIG_OPS[op], step.get("args"), store)
                store = copy.deepcopy(result)
            outputs.append({"result": normalize(result)})
        except JsError as error:
            outputs.append({"error": error_dict(error)})
    return outputs


PACK_CONFIG_OPS = {
    "updateDraftSection": pc.update_draft_section,
    "updateDraftTemplate": pc.update_draft_template,
    "submitDraft": pc.submit_draft,
    "approveDraft": pc.approve_draft,
    "approveDraftDirect": pc.approve_draft_direct,
    "rejectDraft": pc.reject_draft,
    "resetDraft": pc.reset_draft,
}

REGISTRY_OPS = {
    "approvePackage": pr.approve_package,
    "rejectPackage": pr.reject_package,
    "resetPackage": pr.reset_package,
    "bulkApprovePackages": pr.bulk_approve_packages,
    "listPackagesForRole": pr.list_packages_for_role,
    "assertPackageSelectableByActor": pr.assert_package_selectable_by_actor,
    "createPackage": pr.create_package,
    "duplicatePackage": pr.duplicate_package,
    "createRevision": pr.create_revision,
    "editPackage": pr.edit_package,
    "submitPackage": pr.submit_package,
    "archivePackage": pr.archive_package,
}


def _registry_scenario(args: list, local: dict, id_factory: dict) -> list:
    registry, steps = args
    factory = pr.PackageIdFactory(now_ms=id_factory["nowMs"], seq=id_factory["seq"])
    outputs = []
    for step in steps:
        op = step["op"]
        if op == "setValidation":
            package = next(p for p in registry["packages"] if p.get("packageId") == step["packageId"])
            package["lastValidation"] = copy.deepcopy(step["validation"])
            outputs.append({"result": None, "registry": normalize(registry)})
            continue
        try:
            if op == "approvalSummary":
                result = pr.approval_summary(registry)
            else:
                func = REGISTRY_OPS[op]
                extra = {"id_factory": factory} if "id_factory" in inspect.signature(func).parameters else {}
                result = call_with_object(func, resolve(step.get("args"), local), registry, **extra)
            outputs.append({"result": normalize(result), "registry": normalize(registry)})
        except JsError as error:
            outputs.append({"error": error_dict(error), "registry": normalize(registry)})
    return outputs


def _rate_sequence(args: list) -> list:
    card, steps = args
    history = []
    for step in steps:
        entry = {"op": "setRate", "args": step}
        try:
            card = call_with_object(rc.set_rate, step, card)
            entry["result"] = normalize(card)
        except JsError as error:
            entry["error"] = error_dict(error)
            if entry["error"]["message"].startswith(f"{error.code}:"):
                entry["error"]["code"] = None  # the JS threw a plain Error carrying the code in its message
        history.append(entry)
    return history


def _find_offer(args: list, now: str) -> Any:
    offers, criteria = args
    date = criteria.get("date") if truthy(criteria.get("date")) else now[:10]
    return of.find_applicable_offer(offers, system_type=criteria.get("systemType", UNDEFINED), tier=criteria.get("tier", UNDEFINED), size=criteria.get("size", UNDEFINED), date=date)


def _auto_expire(args: list, now: str) -> dict:
    offers, as_of = args
    expired = of.auto_expire_offers(offers, as_of if truthy(as_of) else None, now=now)
    return {"expired": expired, "offers": offers}


def _allocate_landed(obj: dict) -> dict:
    extra = {}
    if "allocationMethod" in obj:
        extra["allocation_method"] = obj["allocationMethod"]
    return ce.allocate_landed(
        obj.get("lines", []),
        obj.get("charges", []),
        batch_id=obj.get("batchId"),
        recorded_by=obj.get("recordedBy"),
        recorded_at=obj.get("recordedAt"),
        effective_from=obj.get("effectiveFrom"),
        version_id=obj.get("versionId"),
        **extra,
    )


def _archive_version(args: list) -> dict:
    store, obj = args
    return call_with_object(rc.archive_version, obj, store)


def run_case(module: str, case: dict) -> Any:
    """Run one golden case on the Python engine; returns the normalised result (raises on an unexpected error)."""
    local = load_golden(module).get("fixtures", {})
    now = fixed_now(module)
    fn = case["fn"]
    if fn in ("buildBom", "buildAllTierBoms", "getAlternatives"):
        func = {"buildBom": bb.build_bom, "buildAllTierBoms": bb.build_all_tier_boms, "getAlternatives": bb.get_alternatives}[fn]
        return func(resolve(case["args"][0], local), **_bom_env(case, local))
    args = resolve(case["args"], local)
    if fn == "packConfigScenario":
        return _pack_config_scenario(args, now)
    if fn == "registryScenario":
        return _registry_scenario(args, local, local["idFactory"])
    if fn == "setRateSequence":
        return _rate_sequence(args)
    handler = ADAPTERS[fn]
    return handler(args, now)


ADAPTERS: dict[str, Callable[[list, str], Any]] = {
    # money.js
    "roundMoney": lambda a, now: round_money(_arg(a, 0)),
    "isMoney": lambda a, now: is_money(_arg(a, 0)),
    "mulExact": lambda a, now: mul_exact(_arg(a, 0), _arg(a, 1)),
    "sumExact": lambda a, now: sum_exact(_arg(a, 0)),
    # deviceAllocation.js
    "allocateDevices": lambda a, now: call_with_object(da.allocate_devices, a[0]),
    # server-bom-builder.js
    "getProfileKey": lambda a, now: bb.get_profile_key(_arg(a, 0), _arg(a, 1)),
    "isSalesRole": lambda a, now: bb.is_sales_role(_arg(a, 0)),
    "stripCostFieldsForSales": lambda a, now: bb.strip_cost_fields_for_sales(a[0]),
    # packPricing.js
    "pricePack": lambda a, now: call_with_object(pp.price_pack, a[0]),
    "structureMaterial": lambda a, now: pp.structure_material(_arg(a, 0), _arg(a, 1), _arg(a, 2)),
    "structureQty": lambda a, now: pp.structure_qty(_arg(a, 0), _arg(a, 1)),
    "tubeRateFor": lambda a, now: pp.tube_rate_for(_arg(a, 0), _arg(a, 1)),
    "kwOf": lambda a, now: pp.kw_of(_arg(a, 0)),
    "marketRateKey": lambda a, now: call_with_object(pc.market_rate_key, a[0]),
    # packConfig.js
    "updateDraftSectionDescribe": lambda a, now: pc.describe_store(call_with_object(pc.update_draft_section, a[1], a[0])),
    "seedFromCatalog": lambda a, now: call_with_object(pc.seed_from_catalog, a[1], a[0]),
    "submitDraft": lambda a, now: call_with_object(pc.submit_draft, a[1], a[0]),
    "approveDraftDirect": lambda a, now: call_with_object(pc.approve_draft_direct, a[1], a[0]),
    "describeStore": lambda a, now: pc.describe_store(a[0]),
    "approvedSizes": lambda a, now: pc.approved_sizes(_arg(a, 0), _arg(a, 1)),
    "futurePairsFor": lambda a, now: pc.future_pairs_for(_arg(a, 0), _arg(a, 1), _arg(a, 2)),
    # packageApproval.js / packageAuthority.js / packageProjection.js
    "comboKeyFor": lambda a, now: pr.combo_key_for(a[0]),
    "resolvePackageArchitecture": lambda a, now: pr.resolve_package_architecture(_arg(a, 0), _arg(a, 1)),
    "hydrateRegistry": lambda a, now: pr.hydrate_registry(_arg(a, 0)),
    "hydrateThenVisible": lambda a, now: pr.visible_for_runtime(pr.hydrate_registry(_arg(a, 0))),
    "visibleForRuntime": lambda a, now: pr.visible_for_runtime(_arg(a, 0)),
    "derivePackageComponents": lambda a, now: call_with_object(pr.derive_package_components, _arg(a, 2), _arg(a, 0), _arg(a, 1)),
    "isApprovedSelection": lambda a, now: pr.is_approved_selection(_arg(a, 0), _arg(a, 1), _arg(a, 2)),
    "isSalesEditable": lambda a, now: pr.is_sales_editable(_arg(a, 0)),
    # costEngine.js / procurementPriceMaster.js
    "calculateCost": lambda a, now: call_with_object(ce.calculate_cost, a[0]),
    "explainCost": lambda a, now: ce.explain_cost(_arg(a, 0)),
    "toPriceRecord": lambda a, now: ce.to_price_record(_arg(a, 0)),
    "projectUnitCost": lambda a, now: ce.project_unit_cost(ce.to_price_record(a[0]) if a[0] is not None else None),
    "procurementUplift": lambda a, now: ce.procurement_uplift(ce.to_price_record(_arg(a, 0))),
    "buildPriceMaster": lambda a, now: ce.build_price_master(_arg(a, 0)),
    # procurementBatch.js
    "allocateLanded": lambda a, now: _allocate_landed(a[0]),
    "buildBatchLandedCosts": lambda a, now: ce.build_batch_landed_costs(_arg(a, 0)),
    "toPriceMasterEntries": lambda a, now: call_with_object(ce.to_price_master_entries, _arg(a, 1), _arg(a, 0)),
    # pricingEngine.js
    "calculatePricing": lambda a, now: call_with_object(pe.calculate_pricing, a[0]),
    "resolveTierMargin": lambda a, now: pe.resolve_tier_margin(_arg(a, 0), _arg(a, 1)),
    "resolveGstRegime": lambda a, now: pe.resolve_gst_regime(_arg(a, 0)),
    "validateGrossMargin": lambda a, now: pe.validate_gross_margin(_arg(a, 0)),
    "explainPricing": lambda a, now: pe.explain_pricing(_arg(a, 0)),
    # offerLifecycle.js
    "validateOffer": lambda a, now: of.validate_offer(a[0]),
    "marginSafetyCheck": lambda a, now: of.margin_safety_check(*a),
    "isValidTransition": lambda a, now: of.is_valid_transition(_arg(a, 0), _arg(a, 1)),
    "transitionOffer": lambda a, now: of.transition_offer(a[0], a[1], a[2], _arg(a, 3), now=now),
    "createDraftOffer": lambda a, now: of.create_draft_offer(a[0], a[1], now=now),
    "updateOffer": lambda a, now: of.update_offer(a[0], a[1], a[2], now=now),
    "autoExpireOffers": _auto_expire,
    "findApplicableOffer": _find_offer,
    "calculateOfferAmount": lambda a, now: of.calculate_offer_amount(_arg(a, 0), _arg(a, 1)),
    "offerPayloadShape": lambda a, now: of.offer_payload_shape(_arg(a, 0), _arg(a, 1)),
    # batteryMaster.js / batteryCompatibility.js / resolveBattery.js
    "toBatteryMaster": lambda a, now: bc.to_battery_master(_arg(a, 0), _arg(a, 1)),
    "allBatteries": lambda a, now: bc.all_batteries(_arg(a, 0), _arg(a, 1)),
    "isSelectable": lambda a, now: bc.is_selectable(_arg(a, 0)),
    "isPermanentlyUnselectable": lambda a, now: bc.is_permanently_unselectable(_arg(a, 0)),
    "selectabilityReason": lambda a, now: bc.selectability_reason(_arg(a, 0)),
    "resolveProtectionRequirement": lambda a, now: bc.resolve_protection_requirement(_arg(a, 0)),
    "checkBatteryCompatibility": lambda a, now: bc.check_battery_compatibility(_arg(a, 0), _arg(a, 1)),
    "resolveBattery": lambda a, now: bc.resolve_battery(_arg(a, 0), _arg(a, 1), _arg(a, 2)),
    "approvedBatteryShortlist": lambda a, now: bc.approved_battery_shortlist(_arg(a, 0), _arg(a, 1), _arg(a, 2)),
    "isBatteryAlias": lambda a, now: bc.is_battery_alias(_arg(a, 0), _arg(a, 1)),
    "canonicalBatteryIdFor": lambda a, now: bc.canonical_battery_id_for(_arg(a, 0), _arg(a, 1)),
    # commercialHistory.js / projectRateCard.js
    "seedRateCardFromConfig": lambda a, now: call_with_object(rc.seed_rate_card_from_config, a[1], a[0]),
    "resolveRate": lambda a, now: call_with_object(rc.resolve_rate, a[1], a[0]),
    "archiveVersion": lambda a, now: _archive_version(a),
    "getHistory": lambda a, now: rc.get_history(*a),
    "getVersion": lambda a, now: rc.get_version(*a),
    "compareVersions": lambda a, now: rc.compare_versions(_arg(a, 0), _arg(a, 1)),
    "historyView": lambda a, now: rc.history_view(*a),
    "listRates": lambda a, now: rc.list_rates(_arg(a, 0)),
    "installationKey": lambda a, now: rc.installation_key(_arg(a, 0), _arg(a, 1)),
    "engineeringKey": lambda a, now: rc.engineering_key(_arg(a, 0), _arg(a, 1)),
}


# ---------------------------------------------------------------------------------------------------------------
# Comparison
# ---------------------------------------------------------------------------------------------------------------


def normalize(value: Any) -> Any:
    """The JSON a JS engine would have produced for this Python value (``JSON.parse(JSON.stringify(v))``)."""
    if value is UNDEFINED:
        return {"$js": "undefined"}
    return _normalize(value)


def _normalize(value: Any) -> Any:
    if isinstance(value, dict):
        return {str(k): _normalize(v) for k, v in value.items() if v is not UNDEFINED}
    if isinstance(value, (list, tuple)):
        return [None if v is UNDEFINED else _normalize(v) for v in value]
    if isinstance(value, float) and not math.isfinite(value):
        return None
    if isinstance(value, Decimal):
        return float(value)
    if isinstance(value, re.Pattern):
        return {}
    return value


def error_dict(error: JsError) -> dict:
    return {"name": error.js_name, "message": error.message, "code": error.code, "detail": normalize(error.detail) if error.detail is not None else None}


def assert_same(expected: Any, actual: Any, path: str = "$") -> None:
    """Exact JSON equality; booleans never equal numbers, numbers compare by value (1 == 1.0)."""
    if isinstance(expected, bool) or isinstance(actual, bool):
        assert isinstance(expected, bool) and isinstance(actual, bool) and expected == actual, f"{path}: expected {expected!r}, got {actual!r}"
    elif expected is None or actual is None:
        assert expected is None and actual is None, f"{path}: expected {expected!r}, got {actual!r}"
    elif isinstance(expected, (int, float)):
        assert isinstance(actual, (int, float)) and expected == actual, f"{path}: expected {expected!r}, got {actual!r}"
    elif isinstance(expected, str):
        assert isinstance(actual, str) and expected == actual, f"{path}: expected {expected!r}, got {actual!r}"
    elif isinstance(expected, list):
        assert isinstance(actual, list), f"{path}: expected a list, got {actual!r}"
        assert len(expected) == len(actual), f"{path}: expected {len(expected)} items, got {len(actual)}"
        for i, (e, a) in enumerate(zip(expected, actual)):
            assert_same(e, a, f"{path}[{i}]")
    elif isinstance(expected, dict):
        assert isinstance(actual, dict), f"{path}: expected an object, got {actual!r}"
        missing = sorted(set(expected) - set(actual))
        extra = sorted(set(actual) - set(expected))
        assert not missing and not extra, f"{path}: missing keys {missing}, unexpected keys {extra}"
        for key in expected:
            assert_same(expected[key], actual[key], f"{path}.{key}")
    else:
        raise AssertionError(f"{path}: unexpected golden value {expected!r}")


def assert_error_same(expected: dict, actual: dict) -> None:
    """Errors match on name, message and detail; a JS error without a code may carry its message prefix as code."""
    code = actual["code"]
    if expected["code"] is None and code is not None:
        assert expected["message"].startswith(f"{code}:"), f"code {code!r} is not the prefix of {expected['message']!r}"
        actual = {**actual, "code": None}
    assert_same(expected, actual)
