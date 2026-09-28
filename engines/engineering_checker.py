"""Engineering rules: the server checker (PBC-*) and the configuration validation (ENG-*), rule sets held as data.

Ports of ``engineeringChecker.js`` (``checkProjectBom``, ``RULES_VERSION 'phase1e.1'``, engines spec §11b) with its
dependencies ``batteryMaster.js`` (``toBatteryMaster``), ``batteryCompatibility.js`` (checks BC-A…BC-J,
``resolveProtectionRequirement``) and ``upgradeModel.js``; and of ``engineeringValidation.js`` (``validateEngineering``,
§11a) with ``scripts/validate-bom.mjs validateBatteryConsistency``.

**Rules as data.** Every rule — code, category, severity, description, inputs, source, whether a WARNING needs a named
acknowledgement before a BOM lock, and its parameters (the 15 °C Kerala cell temperature, the D11 isolators, the
required roles, the approved upgrade paths, …) — lives in a :class:`RuleSet`. :data:`DEFAULT_RULE_SET` is the legacy
register exactly (35 PBC rules; PLAN §2.5 says 33, the JavaScript register holds 35 and ``checksRun`` reports 35);
``RuleSet.as_json()`` is the document stored in ``engineering_rule_set.rules`` and ``RuleSet.from_json`` validates a
stored one (every code once, known severities, typed parameters). The evaluation logic is code; what a rule set may
change is its severities and parameters.

**Vocabulary.** Severities are the PLAN's ``BLOCK``/``WARN``/``INFO`` (:class:`Severity`); a run's verdict is
``VALID``/``WARNING``/``BLOCKED`` (:class:`CheckStatus`, the quotation payload's vocabulary) and ``PASS``/``WARN``/
``FAIL`` for ``engineering_run.result`` (:attr:`CheckResult.result`). ``as_dict()`` gives the JavaScript result object
key for key (``BLOCKED``/``WARNING`` severities), which is what the golden files compare and what the quotation BOM
snapshot freezes; :meth:`Finding.as_row` is the ``engineering_finding`` row.

Read-only over the BOM, deterministic (the caller supplies ``at``), findings sorted by rule then component ids.
"""

from __future__ import annotations

import re
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass, field
from decimal import Decimal, localcontext
from enum import StrEnum
from typing import Any

from engines.bom_domain import CATEGORY_TO_ROLE, ENPHASE_COMPONENT_ROLES, ComponentStatus, Role, classify_catalog_item, effective_lines
from engines.frozen import FrozenDict, deep_freeze
from engines.jscompat import (
    EXACT,
    UNDEFINED,
    coalesce,
    is_nullish,
    is_number,
    js_array,
    js_finite_number,
    js_keys,
    js_number,
    js_string,
    js_truthy,
    number_text,
    prop,
    round_places,
    to_decimal,
    to_fixed,
)

__all__ = [
    "Severity",
    "CheckStatus",
    "RunResult",
    "Rule",
    "RuleSet",
    "RuleSetError",
    "RULES_VERSION",
    "CATEGORY",
    "DEFAULT_RULE_SET",
    "VALIDATION_RULE_SET",
    "VALIDATION_RULES_VERSION",
    "Finding",
    "Counts",
    "CheckResult",
    "check_project_bom",
    "check_project_bom_js",
    "ValidationFinding",
    "ValidationResult",
    "validate_engineering",
    "validate_battery_consistency",
    "BatteryProtectionMode",
    "EngineeringStatus",
    "ProcurementStatus",
    "BATTERY_FIELDS",
    "REQUIRED_FOR_VALIDATION",
    "REQUIRED_FOR_SAFE_ISSUE",
    "to_battery_master",
    "CompatResult",
    "COMPAT_CHECKS",
    "check_battery_compatibility",
    "resolve_protection_requirement",
    "APPROVED_UPGRADE_PATHS",
    "MANDATORY_UPGRADE_SECTIONS",
    "OPTIONAL_UPGRADE_SECTIONS",
    "build_upgrade_identity",
    "upgrade_identity_key",
    "check_upgrade_section_policy",
    "registry_lines",
]


# ---------------------------------------------------------------------------------------------------------------------
# Vocabulary
# ---------------------------------------------------------------------------------------------------------------------


class Severity(StrEnum):
    """``engineering_finding.severity`` (PLAN §2.5). ``legacy`` is the JavaScript word."""

    BLOCK = "BLOCK"
    WARN = "WARN"
    INFO = "INFO"

    @property
    def legacy(self) -> str:
        return {"BLOCK": "BLOCKED", "WARN": "WARNING", "INFO": "INFO"}[self.value]

    @classmethod
    def parse(cls, value: Any) -> Severity:
        """Accepts the PLAN word or the JavaScript word (``BLOCKED``/``WARNING``)."""
        text = {"BLOCKED": "BLOCK", "WARNING": "WARN"}.get(value, value) if isinstance(value, str) else value
        return cls(text)


class CheckStatus(StrEnum):
    """The verdict of a run (the quotation payload and gate vocabulary)."""

    VALID = "VALID"
    WARNING = "WARNING"
    BLOCKED = "BLOCKED"

    @property
    def result(self) -> RunResult:
        return {"VALID": RunResult.PASS, "WARNING": RunResult.WARN, "BLOCKED": RunResult.FAIL}[self.value]


class RunResult(StrEnum):
    """``engineering_run.result`` (PLAN §2.5)."""

    PASS = "PASS"
    WARN = "WARN"
    FAIL = "FAIL"


class RuleSetError(ValueError):
    """A rule-set document that cannot be used: ``errors`` lists every problem as ``{path, message}``."""

    def __init__(self, message: str, errors: Sequence[Mapping[str, str]] = ()) -> None:
        super().__init__(message)
        self.code = "rule_set_invalid"
        self.errors = tuple(errors)


# ---------------------------------------------------------------------------------------------------------------------
# Parameter schemas (what a stored rule set may set, and how it is checked)
# ---------------------------------------------------------------------------------------------------------------------


def _is_text_list(value: Any) -> bool:
    return isinstance(value, (list, tuple)) and all(isinstance(item, str) and item for item in value)


def _is_role_list(value: Any) -> bool:
    return _is_text_list(value) and all(item in {role.value for role in Role} or item == "DC_ISOLATOR" for item in value)


def _is_real(value: Any) -> bool:
    """A finite number (``int``, finite ``Decimal``, finite ``float``); NaN and the infinities are not parameters."""
    if is_number(value):
        return not isinstance(value, Decimal) or value.is_finite()
    return isinstance(value, float) and to_decimal(value).is_finite()


def _is_positive(value: Any) -> bool:
    return _is_real(value) and js_number(value) > 0


def _is_text_map(value: Any) -> bool:
    return isinstance(value, Mapping) and all(isinstance(key, str) and isinstance(item, str) and item for key, item in value.items())


def _is_role_map(value: Any) -> bool:
    return isinstance(value, Mapping) and all(isinstance(key, str) and _is_role_list(item) for key, item in value.items())


def _is_upgrade_paths(value: Any) -> bool:
    if not isinstance(value, (list, tuple)) or not value:
        return False
    for path in value:
        if not isinstance(path, Mapping) or not isinstance(path.get("id"), str):
            return False
        if not (_is_positive(path.get("fromKw")) and _is_positive(path.get("toKw")) and isinstance(path.get("explicitBom"), bool)):
            return False
        if "note" in path and not (path["note"] is None or isinstance(path["note"], str)):
            return False
    return True


_PARAM_CHECKS: Mapping[str, tuple[Callable[[Any], bool], str]] = {
    "text": (lambda value: isinstance(value, str) and bool(value), "a non-empty string"),
    "number": (_is_real, "a number"),
    "positive": (_is_positive, "a positive number"),
    "texts": (_is_text_list, "a list of non-empty strings"),
    "roles": (_is_role_list, "a list of BOM roles"),
    "textMap": (_is_text_map, "an object of non-empty strings"),
    "roleMap": (_is_role_map, "an object of role lists"),
    "upgradePaths": (_is_upgrade_paths, "a non-empty list of {id, fromKw, toKw, explicitBom, note?}"),
}


# ---------------------------------------------------------------------------------------------------------------------
# Rules and rule sets
# ---------------------------------------------------------------------------------------------------------------------


@dataclass(frozen=True)
class Rule:
    """One rule of a rule set. ``params`` are typed per rule (see the rule's schema in its engine)."""

    code: str
    category: str
    severity: Severity
    description: str
    inputs: tuple[str, ...]
    source: str
    requires_acknowledgement: bool = False
    params: Mapping[str, Any] = field(default_factory=FrozenDict)

    def param(self, name: str) -> Any:
        return self.params[name]

    def as_json(self) -> dict[str, Any]:
        return {
            "code": self.code,
            "category": self.category,
            "severity": self.severity.value,
            "description": self.description,
            "inputs": list(self.inputs),
            "source": self.source,
            "requiresAcknowledgement": self.requires_acknowledgement,
            "params": _plain(self.params),
        }


def _plain(value: Any) -> Any:
    """JSONB-ready parameters: containers as dict/list, a Decimal as ``int`` when whole (else ``float``)."""
    if isinstance(value, Mapping):
        return {key: _plain(item) for key, item in value.items()}
    if isinstance(value, (list, tuple)):
        return [_plain(item) for item in value]
    if isinstance(value, Decimal):
        return int(value) if value == value.to_integral_value() else float(value)
    return value


@dataclass(frozen=True)
class RuleSet:
    """A versioned, complete rule register for one engine (``engineeringChecker`` or ``engineeringValidation``)."""

    engine: str
    version: str
    rules: tuple[Rule, ...]

    def __post_init__(self) -> None:
        object.__setattr__(self, "_by_code", {rule.code: rule for rule in self.rules})

    def rule(self, code: str) -> Rule:
        return self._by_code[code]  # type: ignore[attr-defined]

    @property
    def codes(self) -> tuple[str, ...]:
        return tuple(rule.code for rule in self.rules)

    def as_json(self) -> dict[str, Any]:
        """The ``engineering_rule_set.rules`` document."""
        return {"engine": self.engine, "version": self.version, "rules": [rule.as_json() for rule in self.rules]}

    def with_changes(self, *, version: str, severities: Mapping[str, Any] | None = None, params: Mapping[str, Mapping[str, Any]] | None = None) -> RuleSet:
        """A new, validated rule set: ``severities`` (``{code: severity}``) and ``params`` (``{code: {name: value}}``) replaced.

        A change naming a rule this set does not hold, a severity that is not BLOCK/WARN/INFO or parameters that are not
        an object is refused (:class:`RuleSetError`) instead of producing a new version that silently equals this one."""
        errors: list[dict[str, str]] = []
        for label, changes in (("severities", severities), ("params", params)):
            if changes is not None and not isinstance(changes, Mapping):
                errors.append({"path": label, "message": "must be an object keyed by rule code"})
                continue
            for code in changes or {}:
                if code not in self._by_code:  # type: ignore[attr-defined]
                    errors.append({"path": f"{label}.{code}", "message": f"unknown rule {code!r}"})
                elif label == "severities":
                    try:
                        Severity.parse(changes[code])
                    except ValueError:
                        errors.append({"path": f"{label}.{code}", "message": "must be BLOCK, WARN or INFO"})
                elif not isinstance(changes[code], Mapping):
                    errors.append({"path": f"{label}.{code}", "message": "must be an object of parameters"})
        if errors:
            raise RuleSetError("The rule set changes are invalid.", errors)
        document = self.as_json()
        document["version"] = version
        for rule in document["rules"]:
            if severities and rule["code"] in severities:
                rule["severity"] = Severity.parse(severities[rule["code"]]).value
            if params and rule["code"] in params:
                rule["params"] = {**rule["params"], **params[rule["code"]]}
        return RuleSet.from_json(document)

    @staticmethod
    def from_json(document: Any) -> RuleSet:
        """Validate and load a stored rule-set document; :class:`RuleSetError` lists every problem."""
        errors: list[dict[str, str]] = []
        if not isinstance(document, Mapping):
            raise RuleSetError("The rule set must be an object.", [{"path": "", "message": "must be an object"}])
        engine = document.get("engine")
        registry = _REGISTRIES.get(engine) if isinstance(engine, str) else None
        if registry is None:
            raise RuleSetError("Unknown rule engine.", [{"path": "engine", "message": f"must be one of {', '.join(sorted(_REGISTRIES))}"}])
        version = document.get("version")
        if not isinstance(version, str) or not 0 < len(version) <= 16:
            errors.append({"path": "version", "message": "must be a string of 1–16 characters"})
        rules_in = document.get("rules")
        if not isinstance(rules_in, (list, tuple)):
            raise RuleSetError("The rule set has no rule list.", [*errors, {"path": "rules", "message": "must be a list"}])
        defaults = registry.default
        seen: dict[str, int] = {}
        rules: list[Rule] = []
        for index, raw in enumerate(rules_in):
            path = f"rules[{index}]"
            if not isinstance(raw, Mapping):
                errors.append({"path": path, "message": "must be an object"})
                continue
            code = raw.get("code")
            if not isinstance(code, str) or code not in defaults._by_code:  # type: ignore[attr-defined]
                errors.append({"path": f"{path}.code", "message": f"unknown rule {code!r}"})
                continue
            if code in seen:
                errors.append({"path": f"{path}.code", "message": f"{code} is listed twice"})
                continue
            seen[code] = index
            base: Rule = defaults.rule(code)
            try:
                severity = Severity.parse(raw.get("severity"))
            except ValueError:
                errors.append({"path": f"{path}.severity", "message": "must be BLOCK, WARN or INFO"})
                severity = base.severity
            category = raw.get("category", base.category)
            if category != base.category:
                errors.append({"path": f"{path}.category", "message": f"must be {base.category}"})
            texts = {}
            for key in ("description", "source"):
                value = raw.get(key, getattr(base, key))
                if not isinstance(value, str) or not value:
                    errors.append({"path": f"{path}.{key}", "message": "must be a non-empty string"})
                    value = getattr(base, key)
                texts[key] = value
            inputs = raw.get("inputs", list(base.inputs))
            if not _is_text_list(inputs):
                errors.append({"path": f"{path}.inputs", "message": "must be a list of strings"})
                inputs = base.inputs
            acknowledgement = raw.get("requiresAcknowledgement", base.requires_acknowledgement)
            if not isinstance(acknowledgement, bool):
                errors.append({"path": f"{path}.requiresAcknowledgement", "message": "must be true or false"})
                acknowledgement = base.requires_acknowledgement
            params_in = raw.get("params", {})
            schema = registry.params.get(code, {})
            params: dict[str, Any] = dict(base.params)
            if not isinstance(params_in, Mapping):
                errors.append({"path": f"{path}.params", "message": "must be an object"})
                params_in = {}
            for name, value in params_in.items():
                if name not in schema:
                    errors.append({"path": f"{path}.params.{name}", "message": "unknown parameter"})
                    continue
                check, expected = _PARAM_CHECKS[schema[name]]
                if not check(value):
                    errors.append({"path": f"{path}.params.{name}", "message": f"must be {expected}"})
                    continue
                params[name] = value
            rules.append(Rule(code, category, severity, texts["description"], tuple(inputs), texts["source"], acknowledgement, deep_freeze(params)))
        missing = [code for code in defaults.codes if code not in seen]
        if missing:
            errors.append({"path": "rules", "message": f"missing rule(s): {', '.join(missing)}"})
        if errors:
            raise RuleSetError("The rule set is invalid.", errors)
        order = {code: position for position, code in enumerate(defaults.codes)}
        return RuleSet(engine, version, tuple(sorted(rules, key=lambda rule: order[rule.code])))


@dataclass(frozen=True)
class _Registry:
    default: RuleSet
    params: Mapping[str, Mapping[str, str]]


_REGISTRIES: dict[str, _Registry] = {}


def _rule(code: str, category: str, severity: str, description: str, inputs: Sequence[str], source: str, *, ack: bool = False, **params: Any) -> Rule:
    return Rule(code, category, Severity.parse(severity), description, tuple(inputs), source, ack, deep_freeze(params))


# ---------------------------------------------------------------------------------------------------------------------
# upgradeModel.js — engineering-approved upgrade paths (decision C10) and the section policy (D10)
# ---------------------------------------------------------------------------------------------------------------------

APPROVED_UPGRADE_PATHS: tuple[Mapping[str, Any], ...] = deep_freeze(
    [
        {"id": "UPG-3-5", "fromKw": 3, "toKw": 5, "explicitBom": True},
        {"id": "UPG-3-6", "fromKw": 3, "toKw": 6, "explicitBom": False, "note": 'approved but bomTemplates.upgrade has no "3_6" fixedItems; currently auto-scaled'},
        {"id": "UPG-3-8", "fromKw": 3, "toKw": 8, "explicitBom": False, "note": 'approved but bomTemplates.upgrade has no "3_8" fixedItems; currently auto-scaled'},
        {"id": "UPG-5-8", "fromKw": 5, "toKw": 8, "explicitBom": True},
        {"id": "UPG-5-10", "fromKw": 5, "toKw": 10, "explicitBom": True},
        {"id": "UPG-8-10", "fromKw": 8, "toKw": 10, "explicitBom": True},
    ]
)
MANDATORY_UPGRADE_SECTIONS = ("panels", "structure", "inverter", "wiring")
OPTIONAL_UPGRADE_SECTIONS = ("hybridInv", "battery")


def _find_path(paths: Sequence[Mapping], from_kw: Any, to_kw: Any) -> Mapping | None:
    """``APPROVED_UPGRADE_PATHS.find(p => p.fromKw === from && p.toKw === to)`` (numbers compared by value)."""
    for path in paths:
        if from_kw is not None and to_kw is not None and js_number(path["fromKw"]) == from_kw and js_number(path["toKw"]) == to_kw:
            return path
    return None


def build_upgrade_identity(*, from_kw: Any, to_kw: Any, tier: Any, sections: Any = None, paths: Sequence[Mapping] = APPROVED_UPGRADE_PATHS) -> Mapping:
    """``buildUpgradeIdentity``: the identity an upgrade quotation carries (the BOM alone cannot tell 3→8 from 5→10)."""
    source, target = js_number(from_kw), js_number(to_kw)
    path = _find_path(paths, source if source.is_finite() else None, target if target.is_finite() else None)
    with localcontext(EXACT):
        delta = (target - source) if source.is_finite() and target.is_finite() else None
    return deep_freeze(
        {
            "upgradePathId": path["id"] if path else f"UNAPPROVED-{number_text(source)}-{number_text(target)}",
            "fromSize": source if source.is_finite() else None,
            "toSize": target if target.is_finite() else None,
            "deltaKw": round_places(delta, 2) if delta is not None else None,
            "tier": tier,
            "approved": path is not None,
            "explicitBom": path["explicitBom"] if path else False,
            "sections": sections if js_truthy(sections) else None,
            "note": (path.get("note") or None) if path else "Path is not in the approved list.",
        }
    )


def upgrade_identity_key(identity: Mapping) -> str:
    """``upgradeIdentityKey``: two upgrades are the same job only if their full identity matches."""
    return "|".join("" if is_nullish(identity.get(key)) else js_string(identity.get(key)) for key in ("upgradePathId", "fromSize", "toSize", "deltaKw", "tier"))


def check_upgrade_section_policy(sections: Any, mandatory: Sequence[str] = MANDATORY_UPGRADE_SECTIONS) -> Mapping:
    """``checkUpgradeSectionPolicy``: an upgrade quotation is sellable only with every mandatory section selected."""
    missing = [section for section in mandatory if not js_truthy(prop(sections, section))]
    message = f"Upgrade quotation is not sellable: mandatory section(s) not selected — {', '.join(missing)}." if missing else "All mandatory upgrade sections selected."
    return deep_freeze({"sellable": not missing, "missingMandatory": missing, "message": message})


# ---------------------------------------------------------------------------------------------------------------------
# The PBC register (engineeringChecker.js CHECKER_RULES), as data
# ---------------------------------------------------------------------------------------------------------------------

RULES_VERSION = "phase1e.1"

#: Validation categories A–S (Phase 1E brief §6).
CATEGORY: Mapping[str, str] = FrozenDict(
    A="SYSTEM_ARCHITECTURE",
    B="PANEL_INVERTER_COMPATIBILITY",
    C="PANEL_ELECTRICAL_LIMITS",
    D="INVERTER_ELECTRICAL_LIMITS",
    E="BATTERY_INVERTER_COMPATIBILITY",
    F="BATTERY_VOLTAGE",
    G="BATTERY_CURRENT",
    H="BATTERY_PROTECTION",
    I="BATTERY_COMMUNICATION",
    J="PHASE_COMPATIBILITY",
    K="REQUIRED_COMPONENT_PRESENCE",
    L="COMPONENT_QUANTITY",
    M="MICROINVERTER_QUANTITY",
    N="BRANCH_CIRCUIT_CONSTRAINTS",
    O="DCDB_ACDB_CONFIGURATION",
    P="AC_ISOLATOR",
    Q="STRUCTURE",
    R="UPGRADE_CONFIGURATION",
    S="FUTURE_UPGRADE_CONFIGURATION",
)
C = CATEGORY

_PBC_RULES = (
    _rule(
        "PBC-A-001",
        C["A"],
        "BLOCKED",
        "Architecture is not declared for the project BOM",
        ["bom.architecture"],
        "Phase 1E §1 — architecture drives every downstream rule",
    ),
    _rule(
        "PBC-A-002",
        C["A"],
        "BLOCKED",
        "Component belongs to a different system architecture",
        ["bom.architecture", "component.brand"],
        "decision D12 — Enphase and Deye/SEG are separate architectures",
        enphaseOnlyRoles=["BATTERY", "MICRO_INVERTER", "ENERGY_SYSTEM_CONTROLLER"],
    ),
    _rule("PBC-B-001", C["B"], "WARNING", "Panel/inverter compatibility cannot be verified — data missing", ["panel", "inverter"], "catalog fields absent; no threshold assumed"),
    _rule(
        "PBC-B-002",
        C["B"],
        "BLOCKED",
        "Panel maximum system voltage is below the inverter maximum input voltage",
        ["panel.maxSysVoltage", "inverter.maxInputVoltage"],
        "catalog panel.maxSysVoltage",
    ),
    _rule(
        "PBC-C-001",
        C["C"],
        "WARNING",
        "Panel electrical data incomplete — limits cannot be evaluated",
        ["panel.voc", "panel.isc", "panel.tempCoeffVoc"],
        "catalog panel record",
        requiredFields=["voc", "isc", "tempCoeffVoc", "maxSysVoltage"],
    ),
    _rule(
        "PBC-D-001",
        C["D"],
        "WARNING",
        "Inverter electrical window not recorded — limits cannot be evaluated",
        ["inverter.mpptVoltageMin", "inverter.mpptVoltageMax", "inverter.maxInputVoltage"],
        "catalog inverter record",
        ack=True,
    ),
    _rule(
        "PBC-D-002",
        C["D"],
        "BLOCKED",
        "Single-panel Voc at minimum cell temperature already exceeds the inverter maximum input voltage",
        ["panel.voc", "panel.tempCoeffVoc", "inverter.maxInputVoltage"],
        "App.jsx stringConfig — Kerala minimum cell temperature 15 °C, STC 25 °C",
        minCellTemperatureC=15,
        stcTemperatureC=25,
    ),
    _rule(
        "PBC-D-003",
        C["D"],
        "WARNING",
        "String length is not declared, so string voltage and current cannot be verified",
        ["bom.stringConfiguration"],
        "Phase 1E — string configuration is not modelled in V1",
        ack=True,
    ),
    _rule(
        "PBC-E-001",
        C["E"],
        "BLOCKED",
        "Battery is not compatible with the selected inverter or architecture",
        ["battery", "inverter"],
        "batteryCompatibility BC-A / BC-B / BC-J",
    ),
    _rule(
        "PBC-F-001",
        C["F"],
        "BLOCKED",
        "Battery voltage is outside the inverter operating range",
        ["battery.nominalVoltage", "inverter.batteryVoltageMin", "inverter.batteryVoltageMax"],
        "batteryCompatibility BC-C",
    ),
    _rule(
        "PBC-F-002",
        C["F"],
        "WARNING",
        "Battery voltage compatibility cannot be verified — data missing",
        ["battery.nominalVoltage"],
        "batteryCompatibility BC-C INDETERMINATE",
        ack=True,
    ),
    _rule(
        "PBC-G-001",
        C["G"],
        "BLOCKED",
        "Battery current capability is below the inverter requirement",
        ["battery.maximumDischargeCurrent", "inverter.maxBatteryCurrent"],
        "batteryCompatibility BC-D",
    ),
    _rule(
        "PBC-G-002",
        C["G"],
        "WARNING",
        "Battery current compatibility cannot be verified — data missing",
        ["battery.maximumDischargeCurrent"],
        "batteryCompatibility BC-D INDETERMINATE",
        ack=True,
    ),
    _rule(
        "PBC-H-001",
        C["H"],
        "BLOCKED",
        "Battery protection mode is unknown — the BOM cannot be issued safely",
        ["battery.integratedProtection"],
        "C44 — protection is the safety-critical field",
    ),
    _rule(
        "PBC-H-002",
        C["H"],
        "BLOCKED",
        "External battery protection is required but the device rating is not recorded",
        ["battery.externalProtectionRequired", "battery.protectionRating"],
        "D12-E — the rating must not be assumed or derived from Ah",
    ),
    _rule(
        "PBC-H-003",
        C["H"],
        "BLOCKED",
        "External battery protection is required but no protection line is present",
        ["bom.lines", "battery"],
        "D12 — protection follows the battery",
    ),
    _rule(
        "PBC-H-004",
        C["H"],
        "BLOCKED",
        "Generic battery protection present on an INTEGRATED-protection topology",
        ["bom.lines", "battery.protectionMode"],
        "D12 — prohibited on the Enphase architecture",
    ),
    _rule(
        "PBC-I-001",
        C["I"],
        "WARNING",
        "Battery communication compatibility cannot be verified — protocol not recorded",
        ["battery.communicationProtocol"],
        "batteryCompatibility BC-E INDETERMINATE",
    ),
    _rule(
        "PBC-J-001",
        C["J"],
        "BLOCKED",
        "Component phase does not match the system phase",
        ["component.phase", "bom.phase"],
        "App.jsx phaseFilter()",
        phaseAgnosticMarker="HYB",
    ),
    _rule(
        "PBC-K-001",
        C["K"],
        "BLOCKED",
        "A required component role is missing from the BOM",
        ["bom.lines", "bom.architecture"],
        "Phase 1E §6 K — required component presence",
        requiredRoles=["PANEL", "STRUCTURE", "AC_ISOLATOR"],
        architectureRoles={"ENPHASE": ["MICRO_INVERTER"]},
        otherArchitectureRoles=["INVERTER"],
        templateScopeExempt=["STRUCTURE"],
    ),
    _rule("PBC-K-002", C["K"], "BLOCKED", "A BOM line has no component selected", ["bom.lines"], "a role present with componentId null cannot be built"),
    _rule("PBC-K-003", C["K"], "BLOCKED", "The selected component is not in the catalog", ["bom.lines", "catalog"], "componentId must resolve to a catalog record"),
    _rule(
        "PBC-K-004",
        C["K"],
        "BLOCKED",
        "The selected component is not ACTIVE in the catalog",
        ["component.status"],
        "D11-D — TEST/INACTIVE records must not be built",
    ),
    _rule("PBC-L-001", C["L"], "BLOCKED", "Component quantity is zero or negative while the role is present", ["bom.lines.quantity"], "Phase 1E §6 L"),
    _rule(
        "PBC-M-001",
        C["M"],
        "BLOCKED",
        "Micro-inverter quantity does not equal the approved panel quantity",
        ["MICRO_INVERTER.quantity", "PANEL.quantity"],
        "D12 — 1 IQ8P per approved PV panel",
    ),
    _rule(
        "PBC-M-002",
        C["M"],
        "BLOCKED",
        "Enphase premium architecture requires exactly one FlexPhase battery in the V1 package",
        ["BATTERY.quantity"],
        "D12 — 1 FlexPhase battery for the current V1 package",
        batteryQuantity=1,
    ),
    _rule(
        "PBC-M-003",
        C["M"],
        "BLOCKED",
        "Enphase premium architecture requires an IQ System Controller for backup (hybrid only — on-grid has no battery, owner decision 2026-09-13)",
        ["bom.lines", "bom.sysType"],
        "D12 — System Controller mandatory for backup",
        systemTypes=["hybrid"],
        defaultSystemType="hybrid",
    ),
    _rule(
        "PBC-N-001",
        C["N"],
        "WARNING",
        "Branch / circuit constraints cannot be verified — string configuration not modelled in V1",
        ["inverter.mpptCount", "inverter.maxStringsPerMppt", "bom.stringConfiguration"],
        "Phase 1E — declared as unverifiable rather than assumed",
        ack=True,
    ),
    _rule(
        "PBC-O-001",
        C["O"],
        "BLOCKED",
        "DCDB or ACDB phase does not match the system phase",
        ["ACDB.phase", "DCDB.phase", "bom.phase"],
        "App.jsx phaseFilter()",
        roles=["ACDB", "DCDB"],
        phaseAgnosticMarker="HYB",
    ),
    _rule(
        "PBC-P-001",
        C["P"],
        "BLOCKED",
        "AC isolator does not match the approved D11 selection for this phase",
        ["AC_ISOLATOR.componentId", "bom.phase"],
        "decisions D11-A (is1, 1P) and D11-B (is2, 3P)",
        isolatorByPhase={"3P": "is2"},
        decisionByPhase={"3P": "decision D11-B"},
        defaultIsolator="is1",
        defaultDecision="decision D11-A",
    ),
    _rule(
        "PBC-P-002",
        C["P"],
        "BLOCKED",
        "A separate DC isolator role is not modelled in V1",
        ["bom.lines"],
        "decision D11-C",
        prohibitedRoles=["DC_ISOLATOR"],
    ),
    _rule("PBC-Q-001", C["Q"], "BLOCKED", "Structure is missing from the BOM", ["bom.lines"], "Phase 1E §6 Q"),
    _rule(
        "PBC-R-001",
        C["R"],
        "BLOCKED",
        "Upgrade path is not engineering-approved",
        ["upgrade.fromKw", "upgrade.toKw"],
        "APPROVED_UPGRADE_PATHS",
        approvedUpgradePaths=APPROVED_UPGRADE_PATHS,
    ),
    _rule(
        "PBC-R-002",
        C["R"],
        "WARNING",
        "Approved upgrade path has no explicit BOM and is currently auto-scaled",
        ["upgrade.pathId"],
        "APPROVED_UPGRADE_PATHS.explicitBom === false",
        ack=True,
    ),
    _rule(
        "PBC-S-001",
        C["S"],
        "WARNING",
        "Future-upgrade configuration mixes panel-kW and inverter-kW sizing; verify per component",
        ["futureUpgrade"],
        "App.jsx future-upgrade sizing split",
    ),
)
del C

#: The legacy register, as data: what ``engineering_rule_set`` version ``phase1e.1`` holds.
DEFAULT_RULE_SET = RuleSet("engineeringChecker", RULES_VERSION, _PBC_RULES)

_REGISTRIES["engineeringChecker"] = _Registry(
    DEFAULT_RULE_SET,
    {
        "PBC-A-002": {"enphaseOnlyRoles": "roles"},
        "PBC-C-001": {"requiredFields": "texts"},
        "PBC-D-002": {"minCellTemperatureC": "number", "stcTemperatureC": "number"},
        "PBC-J-001": {"phaseAgnosticMarker": "text"},
        "PBC-K-001": {"requiredRoles": "roles", "architectureRoles": "roleMap", "otherArchitectureRoles": "roles", "templateScopeExempt": "roles"},
        "PBC-M-002": {"batteryQuantity": "positive"},
        "PBC-M-003": {"systemTypes": "texts", "defaultSystemType": "text"},
        "PBC-O-001": {"roles": "roles", "phaseAgnosticMarker": "text"},
        "PBC-P-001": {"isolatorByPhase": "textMap", "decisionByPhase": "textMap", "defaultIsolator": "text", "defaultDecision": "text"},
        "PBC-P-002": {"prohibitedRoles": "roles"},
        "PBC-R-001": {"approvedUpgradePaths": "upgradePaths"},
    },
)


# ---------------------------------------------------------------------------------------------------------------------
# batteryMaster.js — master-data shape (nothing fabricated: absent fields are null)
# ---------------------------------------------------------------------------------------------------------------------


class EngineeringStatus(StrEnum):
    DRAFT = "DRAFT"
    PENDING_ENGINEERING_APPROVAL = "PENDING_ENGINEERING_APPROVAL"
    APPROVED = "APPROVED"
    REJECTED = "REJECTED"
    WITHDRAWN = "WITHDRAWN"


class ProcurementStatus(StrEnum):
    ACTIVE = "ACTIVE"
    INACTIVE = "INACTIVE"
    DISCONTINUED = "DISCONTINUED"
    PENDING_SOURCING = "PENDING_SOURCING"


class BatteryProtectionMode(StrEnum):
    INTEGRATED = "INTEGRATED"
    EXTERNAL_REQUIRED = "EXTERNAL_REQUIRED"
    EXTERNAL_OPTIONAL = "EXTERNAL_OPTIONAL"
    UNKNOWN = "UNKNOWN"


BATTERY_FIELDS = (
    "componentId",
    "brand",
    "model",
    "displayName",
    "batteryType",
    "chemistry",
    "nominalVoltage",
    "minVoltage",
    "maxVoltage",
    "capacityKwh",
    "usableCapacityKwh",
    "continuousChargeCurrent",
    "continuousDischargeCurrent",
    "maximumChargeCurrent",
    "maximumDischargeCurrent",
    "peakCurrent",
    "integratedProtection",
    "protectionType",
    "protectionRating",
    "externalProtectionRequired",
    "communicationProtocol",
    "communicationRequired",
    "compatibleInverters",
    "compatibleSystemTypes",
    "compatiblePhases",
    "purchasePrice",
    "sellingPrice",
    "supplier",
    "supplierReference",
    "datasheetUrl",
    "engineeringStatus",
    "procurementStatus",
    "createdAt",
    "updatedAt",
)
REQUIRED_FOR_VALIDATION = ("nominalVoltage", "capacityKwh", "maximumDischargeCurrent", "integratedProtection")
REQUIRED_FOR_SAFE_ISSUE = ("integratedProtection",)


def to_battery_master(item: Any, overlay: Mapping | None = None) -> Mapping | None:
    """``toBatteryMaster``: a catalogue row plus its master-data overlay, projected on the master shape."""
    if not js_truthy(item):
        return None
    source = {**item, **(overlay or {})}
    out: dict[str, Any] = {name: (source[name] if name in source and source[name] is not UNDEFINED else None) for name in BATTERY_FIELDS}
    out["componentId"] = _first_truthy(prop(item, "id"), prop(source, "componentId"))
    out["brand"] = _first_truthy(prop(source, "brand"))
    out["displayName"] = _first_truthy(prop(source, "displayName"), prop(source, "name"))
    out["model"] = _first_truthy(prop(source, "model"))
    purchase = prop(source, "purchasePrice")
    out["purchasePrice"] = purchase if not is_nullish(purchase) else coalesce(prop(source, "price"), None)
    out["engineeringStatus"] = _first_truthy(prop(source, "engineeringStatus")) or EngineeringStatus.PENDING_ENGINEERING_APPROVAL.value
    out["procurementStatus"] = _first_truthy(prop(source, "procurementStatus")) or ProcurementStatus.ACTIVE.value
    for name in ("compatibleInverters", "compatibleSystemTypes", "compatiblePhases"):
        if out[name] is not None and not isinstance(out[name], (list, tuple)):
            out[name] = None
    tiers = js_array(prop(item, "tiers"))
    out["tiers"] = tiers if tiers is not None else []
    out["missingFields"] = [name for name in BATTERY_FIELDS if out[name] is None]
    out["missingForValidation"] = [name for name in REQUIRED_FOR_VALIDATION if out[name] is None]
    out["missingForSafeIssue"] = [name for name in REQUIRED_FOR_SAFE_ISSUE if out[name] is None]
    return deep_freeze(out)


def _first_truthy(*values: Any) -> Any:
    for value in values:
        if js_truthy(value):
            return value
    return None


# ---------------------------------------------------------------------------------------------------------------------
# batteryCompatibility.js — checks BC-A … BC-J (no threshold invented; missing data is INDETERMINATE)
# ---------------------------------------------------------------------------------------------------------------------


class CompatResult(StrEnum):
    PASS = "PASS"
    FAIL = "FAIL"
    INDETERMINATE = "INDETERMINATE"


COMPAT_CHECKS: tuple[Mapping[str, Any], ...] = deep_freeze(
    [
        {"id": "BC-A", "name": "System type compatibility", "needs": ["compatibleSystemTypes"]},
        {"id": "BC-B", "name": "Inverter compatibility", "needs": ["compatibleInverters"]},
        {"id": "BC-C", "name": "Voltage compatibility", "needs": ["nominalVoltage"]},
        {"id": "BC-D", "name": "Current compatibility", "needs": ["maximumDischargeCurrent"]},
        {"id": "BC-E", "name": "Communication compatibility", "needs": ["communicationProtocol"]},
        {"id": "BC-F", "name": "Phase compatibility", "needs": ["compatiblePhases"]},
        {"id": "BC-G", "name": "Protection requirement", "needs": ["integratedProtection"]},
        {"id": "BC-H", "name": "Battery capacity", "needs": ["capacityKwh"]},
        {"id": "BC-I", "name": "Battery quantity", "needs": []},
        {"id": "BC-J", "name": "Manufacturer / architecture compatibility", "needs": []},
    ]
)
_COMPAT_NAMES = {check["id"]: check["name"] for check in COMPAT_CHECKS}


def _js_less(left: Any, right: Any) -> bool | None:
    """``left < right`` (strings lexicographically, otherwise numerically; ``None`` when a side is NaN)."""
    if isinstance(left, str) and isinstance(right, str):
        return left < right
    x, y = js_number(left), js_number(right)
    if x.is_nan() or y.is_nan():
        return None
    return x < y


def _js_ge(left: Any, right: Any) -> bool:
    less = _js_less(left, right)
    return less is False


def _js_le(left: Any, right: Any) -> bool:
    less = _js_less(right, left)
    return less is False


def _includes(container: Any, value: Any) -> bool:
    """``container.includes(value)`` for an array (SameValueZero) or a string (substring)."""
    if isinstance(container, str):
        return js_string(value) in container
    if isinstance(container, (list, tuple)):
        return any(_same_value(item, value) for item in container)
    return False


def _same_value(left: Any, right: Any) -> bool:
    if is_number(left) and is_number(right):
        return left == right
    if is_number(left) or is_number(right) or isinstance(left, bool) or isinstance(right, bool):
        return type(left) is type(right) and left == right
    return left == right


def check_battery_compatibility(battery: Any, context: Mapping | None = None) -> Mapping:
    """``checkBatteryCompatibility`` → ``{status, checks[{id,name,result,message,detail}], reasons, counts}``."""
    context = context or {}
    defaults = {"inverter": None, "sysType": "hybrid", "phase": None, "quantity": 0, "architecture": None}
    inverter, sys_type, phase, quantity, architecture = (defaults[key] if context.get(key, UNDEFINED) is UNDEFINED else context[key] for key in defaults)
    checks: list[dict[str, Any]] = []

    def add(check_id: str, result: CompatResult, message: str, detail: Any = UNDEFINED) -> None:
        checks.append({"id": check_id, "name": _COMPAT_NAMES.get(check_id), "result": result.value, "message": message, "detail": detail})

    if not js_truthy(battery):
        add("BC-A", CompatResult.FAIL, "No battery component supplied.")
        return _summarise(checks, battery)

    battery_brand = js_string(prop(battery, "brand") or "").lower()
    name = js_string(prop(battery, "displayName"))
    if architecture == "ENPHASE" and battery_brand != "enphase":
        brand = prop(battery, "brand")
        add("BC-J", CompatResult.FAIL, f'Enphase premium architecture accepts only an Enphase battery. "{name}" is {js_string(brand) if js_truthy(brand) else "unbranded"}.')
    elif js_truthy(architecture) and architecture != "ENPHASE" and battery_brand == "enphase":
        add("BC-J", CompatResult.FAIL, f'Enphase battery "{name}" cannot be used on a {js_string(architecture)} architecture.')
    else:
        add("BC-J", CompatResult.PASS, "Architecture/manufacturer combination is permitted.")

    system_types = battery["compatibleSystemTypes"]
    if system_types is None:
        add("BC-A", CompatResult.INDETERMINATE, "compatibleSystemTypes not recorded.", {"missing": "compatibleSystemTypes"})
    elif not _includes(system_types, sys_type):
        add("BC-A", CompatResult.FAIL, f'Battery is not listed as compatible with system type "{js_string(sys_type)}".')
    else:
        add("BC-A", CompatResult.PASS, f'Compatible with "{js_string(sys_type)}".')

    inverters = battery["compatibleInverters"]
    inverter_label = js_string(prop(inverter, "name") if js_truthy(prop(inverter, "name")) else prop(inverter, "id"))
    if inverters is None:
        add("BC-B", CompatResult.INDETERMINATE, "compatibleInverters not recorded — inverter compatibility cannot be verified.", {"missing": "compatibleInverters"})
    elif not js_truthy(inverter):
        add("BC-B", CompatResult.INDETERMINATE, "No inverter in context to check against.")
    elif not _includes(inverters, prop(inverter, "id")):
        add("BC-B", CompatResult.FAIL, f'Battery is not approved for inverter "{inverter_label}".', {"inverterId": prop(inverter, "id"), "approved": inverters})
    else:
        add("BC-B", CompatResult.PASS, f'Approved for inverter "{inverter_label}".')

    voltage = battery["nominalVoltage"]
    window_min, window_max = prop(inverter, "batteryVoltageMin"), prop(inverter, "batteryVoltageMax")
    if voltage is None:
        add("BC-C", CompatResult.INDETERMINATE, "nominalVoltage not recorded.", {"missing": "nominalVoltage"})
    elif not is_nullish(window_min) and not is_nullish(window_max):
        ok = _js_ge(voltage, window_min) and _js_le(voltage, window_max)
        message = "Battery voltage is within the inverter window." if ok else f"Battery {js_string(voltage)} V is outside the inverter window {js_string(window_min)}–{js_string(window_max)} V."
        add("BC-C", CompatResult.PASS if ok else CompatResult.FAIL, message)
    else:
        add("BC-C", CompatResult.INDETERMINATE, "Inverter battery-voltage window not recorded.", {"missing": "inverter.batteryVoltageMin/Max"})

    current = battery["maximumDischargeCurrent"]
    required = prop(inverter, "maxBatteryCurrent")
    if current is None:
        add("BC-D", CompatResult.INDETERMINATE, "maximumDischargeCurrent not recorded.", {"missing": "maximumDischargeCurrent"})
    elif not is_nullish(required):
        ok = _js_ge(current, required)
        message = "Battery current capability meets the inverter requirement." if ok else f"Battery {js_string(current)} A is below the inverter requirement {js_string(required)} A."
        add("BC-D", CompatResult.PASS if ok else CompatResult.FAIL, message)
    else:
        add("BC-D", CompatResult.INDETERMINATE, "Inverter max battery current not recorded.", {"missing": "inverter.maxBatteryCurrent"})

    protocol = battery["communicationProtocol"]
    protocols = prop(inverter, "communicationProtocols")
    if battery["communicationRequired"] is False:
        add("BC-E", CompatResult.PASS, "No communication link required.")
    elif protocol is None:
        add("BC-E", CompatResult.INDETERMINATE, "communicationProtocol not recorded.", {"missing": "communicationProtocol"})
    elif is_nullish(protocols):
        add("BC-E", CompatResult.INDETERMINATE, "Inverter communication protocols not recorded.", {"missing": "inverter.communicationProtocols"})
    else:
        ok = _includes(protocols, protocol)
        add("BC-E", CompatResult.PASS if ok else CompatResult.FAIL, f"Protocol {js_string(protocol)} is supported." if ok else f"Inverter does not support {js_string(protocol)}.")

    phases = battery["compatiblePhases"]
    if phases is None:
        add("BC-F", CompatResult.INDETERMINATE, "compatiblePhases not recorded.", {"missing": "compatiblePhases"})
    elif js_truthy(phase) and not _includes(phases, phase):
        add("BC-F", CompatResult.FAIL, f'Battery is not listed for phase "{js_string(phase)}".')
    else:
        add("BC-F", CompatResult.PASS, f"Compatible with {js_string(phase)}." if js_truthy(phase) else "No phase constraint.")

    integrated = battery["integratedProtection"]
    if integrated is None:
        add(
            "BC-G",
            CompatResult.INDETERMINATE,
            "integratedProtection not recorded — the external-protection requirement cannot be determined.",
            {"missing": "integratedProtection", "safetyCritical": True},
        )
    elif integrated is True:
        add("BC-G", CompatResult.PASS, "Battery carries integrated protection; no external device required.")
    elif battery["externalProtectionRequired"] is True:
        if is_nullish(battery["protectionRating"]):
            add(
                "BC-G",
                CompatResult.INDETERMINATE,
                "External protection is required but protectionRating is not recorded. The rating must not be assumed or derived from Ah.",
                {"missing": "protectionRating", "safetyCritical": True},
            )
        else:
            add("BC-G", CompatResult.PASS, f"External protection required at {js_string(battery['protectionRating'])}.")
    else:
        add("BC-G", CompatResult.INDETERMINATE, "externalProtectionRequired not recorded.", {"missing": "externalProtectionRequired"})

    if battery["capacityKwh"] is None:
        add("BC-H", CompatResult.INDETERMINATE, "capacityKwh not recorded — backup sizing cannot be assessed.", {"missing": "capacityKwh"})
    else:
        add("BC-H", CompatResult.PASS, f"Capacity {js_string(battery['capacityKwh'])} kWh recorded.")

    if _js_le(quantity, 0):
        add("BC-I", CompatResult.FAIL, "Battery quantity must be at least 1 when a battery is selected.")
    else:
        add("BC-I", CompatResult.PASS, f"Quantity {js_string(quantity)}.")
    return _summarise(checks, battery)


def _summarise(checks: list[dict[str, Any]], battery: Any) -> Mapping:
    failed = [check for check in checks if check["result"] == CompatResult.FAIL]
    indeterminate = [check for check in checks if check["result"] == CompatResult.INDETERMINATE]
    safety = [check for check in indeterminate if js_truthy(prop(check["detail"], "safetyCritical"))]
    missing_safe = list(prop(battery, "missingForSafeIssue") or []) if js_truthy(battery) else []
    status, reasons = "VALID", []
    if failed:
        status = "BLOCKED"
        reasons.extend(f"{check['id']}: {check['message']}" for check in failed)
    elif safety or missing_safe:
        status = "BLOCKED"
        missing = list(dict.fromkeys([js_string(check["detail"]["missing"]) for check in safety] + [js_string(name) for name in missing_safe]))
        reasons.append("Cannot safely validate this battery configuration — " + ", ".join(missing) + " missing.")
    elif indeterminate:
        status = "WARNING"
        missing = list(dict.fromkeys(js_string(prop(check["detail"], "missing")) for check in indeterminate if js_truthy(prop(check["detail"], "missing"))))
        reasons.append("Engineering data incomplete — review required: " + ", ".join(missing))
    counts = {"fail": len(failed), "indeterminate": len(indeterminate), "pass": len(checks) - len(failed) - len(indeterminate)}
    return deep_freeze({"status": status, "checks": checks, "reasons": reasons, "counts": counts})


def resolve_protection_requirement(battery: Any) -> Mapping:
    """``resolveProtectionRequirement``: the protection topology of the selected battery. Never assumes a rating."""
    if not js_truthy(battery):
        return deep_freeze({"mode": BatteryProtectionMode.UNKNOWN.value, "externalDeviceRequired": False, "rating": None, "reason": "no battery"})
    if prop(battery, "integratedProtection") is True:
        return deep_freeze({"mode": BatteryProtectionMode.INTEGRATED.value, "externalDeviceRequired": False, "rating": None, "reason": "battery carries integrated protection"})
    if prop(battery, "externalProtectionRequired") is True:
        rating = coalesce(prop(battery, "protectionRating"), None)
        reason = "external protection required; rating NOT recorded and must not be assumed" if rating is None else f"external protection required at {js_string(rating)}"
        return deep_freeze({"mode": BatteryProtectionMode.EXTERNAL_REQUIRED.value, "externalDeviceRequired": True, "rating": rating, "reason": reason})
    if prop(battery, "externalProtectionRequired") is False:
        return deep_freeze({"mode": BatteryProtectionMode.EXTERNAL_OPTIONAL.value, "externalDeviceRequired": False, "rating": None, "reason": "external protection explicitly not required"})
    return deep_freeze({"mode": BatteryProtectionMode.UNKNOWN.value, "externalDeviceRequired": False, "rating": None, "reason": "protection data not recorded"})


# ---------------------------------------------------------------------------------------------------------------------
# checkProjectBom — the server authority on whether a project BOM is technically valid
# ---------------------------------------------------------------------------------------------------------------------


@dataclass(frozen=True)
class Finding:
    """One checker finding. ``as_dict()`` is the JavaScript finding; :meth:`as_row` the ``engineering_finding`` row."""

    rule_id: str
    category: str
    severity: Severity
    component_ids: tuple
    message: str
    reason: str
    source: str
    inputs: tuple
    rules_version: str
    catalog_version: Any
    at: Any

    def as_dict(self) -> dict[str, Any]:
        return {
            "ruleId": self.rule_id,
            "category": self.category,
            "severity": self.severity.legacy,
            "componentIds": list(self.component_ids),
            "message": self.message,
            "reason": self.reason,
            "source": self.source,
            "inputs": list(self.inputs),
            "rulesVersion": self.rules_version,
            "catalogVersion": self.catalog_version,
            "at": self.at,
        }

    def as_row(self) -> dict[str, Any]:
        """``engineering_finding``: ``rule_code``, ``severity`` (BLOCK/WARN/INFO), ``message``, ``context``."""
        return {
            "rule_code": self.rule_id,
            "severity": self.severity.value,
            "message": self.message,
            "context": {"category": self.category, "componentIds": list(self.component_ids), "reason": self.reason, "source": self.source, "inputs": list(self.inputs)},
        }


@dataclass(frozen=True)
class Counts:
    blocked: int
    warning: int
    info: int
    checks_run: int

    def as_dict(self) -> dict[str, int]:
        return {"blocked": self.blocked, "warning": self.warning, "info": self.info, "checksRun": self.checks_run}


@dataclass(frozen=True)
class CheckResult:
    """``checkProjectBom``'s result. ``result`` is the ``engineering_run.result`` word."""

    status: CheckStatus
    findings: tuple[Finding, ...]
    counts: Counts
    rules_version: str
    catalog_version: Any
    at: Any
    deterministic_key: str

    @property
    def result(self) -> RunResult:
        return self.status.result

    @property
    def blockers(self) -> tuple[Finding, ...]:
        return tuple(finding for finding in self.findings if finding.severity == Severity.BLOCK)

    @property
    def warnings(self) -> tuple[Finding, ...]:
        return tuple(finding for finding in self.findings if finding.severity == Severity.WARN)

    def summary(self) -> dict[str, Any]:
        """``engineering_run.summary``: counts, rules version and the deterministic key."""
        return {"status": self.status.value, "counts": self.counts.as_dict(), "rulesVersion": self.rules_version, "catalogVersion": self.catalog_version, "deterministicKey": self.deterministic_key}

    def as_dict(self) -> dict[str, Any]:
        return {
            "status": self.status.value,
            "findings": [finding.as_dict() for finding in self.findings],
            "counts": self.counts.as_dict(),
            "rulesVersion": self.rules_version,
            "catalogVersion": self.catalog_version,
            "at": self.at,
            "deterministicKey": self.deterministic_key,
        }

    @staticmethod
    def from_legacy(document: Mapping) -> CheckResult:
        """A stored JavaScript verdict (``lastValidation`` of a Flarize project or package) as a :class:`CheckResult`,
        for importing historic runs into ``engineering_run``/``engineering_finding``. ``as_dict()`` gives it back."""
        findings = tuple(
            Finding(
                item["ruleId"],
                item["category"],
                Severity.parse(item["severity"]),
                tuple(item.get("componentIds") or ()),
                item["message"],
                item["reason"],
                item["source"],
                tuple(item.get("inputs") or ()),
                item.get("rulesVersion"),
                item.get("catalogVersion"),
                item.get("at"),
            )
            for item in document.get("findings") or ()
        )
        counts = document.get("counts") or {}
        return CheckResult(
            CheckStatus(document["status"]),
            findings,
            Counts(counts.get("blocked", 0), counts.get("warning", 0), counts.get("info", 0), counts.get("checksRun", 0)),
            document.get("rulesVersion"),
            document.get("catalogVersion"),
            document.get("at"),
            document.get("deterministicKey", ""),
        )


def check_project_bom_js(arguments: Mapping) -> Mapping:
    """``checkProjectBom`` called the JavaScript way: one argument object ``{bom, catalog, batteryMaster, at,
    catalogVersion, upgrade, futureUpgrade, lines, templateScope}`` in, the frozen JavaScript result object out (the
    callable ``engines.package_registry.run_package_checker`` injects)."""
    lines = arguments.get("lines")
    result = check_project_bom(
        bom=arguments.get("bom"),
        catalog=arguments.get("catalog"),
        battery_master=arguments.get("batteryMaster") or {},
        at=arguments.get("at"),
        catalog_version=arguments.get("catalogVersion"),
        upgrade=arguments.get("upgrade"),
        future_upgrade=arguments.get("futureUpgrade", False),
        lines=lines if isinstance(lines, (list, tuple)) else None,
        template_scope=bool(arguments.get("templateScope", False)),
        rule_set=arguments.get("ruleSet"),
    )
    return deep_freeze(result.as_dict())


def registry_lines(components: Sequence[Mapping]) -> tuple:
    """The lines a package template is checked with (``packageApproval.runPackageChecker``): role from the Enphase id
    map, then the category map (``role`` holds the catalogue category), else OTHER; ``{role, componentId, quantity}``,
    stably sorted by role as ``String(a.role).localeCompare(String(b.role))`` orders them — the order decides which of
    two findings that tie on (rule, components) comes first."""
    lines = []
    for component in components:
        component_id = prop(component, "componentId")
        role = ENPHASE_COMPONENT_ROLES.get(component_id) if isinstance(component_id, str) else None
        if role is None:
            role = CATEGORY_TO_ROLE.get(prop(component, "role")) if isinstance(prop(component, "role"), str) else None
        lines.append({"role": (role or Role.OTHER).value, "componentId": component_id, "quantity": prop(component, "quantity")})
    return deep_freeze(sorted(lines, key=lambda line: _locale_key(line["role"])))


def _locale_key(role: str) -> str:
    """``localeCompare`` (ICU root collation) on role names — capitals and underscores only: the underscore, a
    punctuation mark, sorts before every letter (``AC_CABLE`` < ``ACDB``), unlike its code point."""
    return role.replace("_", " ")


def _catalog_lookup(catalog: Any) -> Callable[[Any], Mapping | None]:
    categories = prop(catalog, "categories")
    ordered = sorted(js_keys(categories)) if js_truthy(categories) else []

    def item(component_id: Any) -> Mapping | None:
        if not js_truthy(component_id) or not js_truthy(categories):
            return None
        for key in ordered:
            for candidate in js_array(prop(categories[key], "items")) or []:
                if _same_value(prop(candidate, "id"), component_id):
                    return FrozenDict({**candidate, "_category": key})
        return None

    return item


def check_project_bom(
    *,
    bom: Any = None,
    catalog: Any = None,
    battery_master: Mapping | None = None,
    at: Any = None,
    catalog_version: Any = None,
    upgrade: Any = None,
    future_upgrade: Any = False,
    lines: Sequence[Mapping] | None = None,
    template_scope: bool = False,
    rule_set: RuleSet | None = None,
) -> CheckResult:
    """``checkProjectBom``: every applicable rule over the BOM's effective lines (or ``lines`` when given).

    ``bom`` carries ``architecture``, ``phase``, ``sysType`` (and, without ``lines``, the project-BOM workspace);
    ``template_scope=True`` (a pack template: structure is chosen per site) skips the STRUCTURE presence rules.
    """
    rules = rule_set or DEFAULT_RULE_SET
    if rules.engine != "engineeringChecker":
        raise RuleSetError("check_project_bom needs an engineeringChecker rule set.")
    battery_master = battery_master or {}
    findings: list[Finding] = []

    def add(rule_id: str, component_ids: Sequence[Any], message: str, reason: Any = None, extra_inputs: Sequence[str] | None = None) -> None:
        rule = rules.rule(rule_id)
        ids = sorted((cid for cid in component_ids if js_truthy(cid)), key=js_string)
        findings.append(
            Finding(
                rule_id,
                rule.category,
                rule.severity,
                tuple(ids),
                message,
                reason if js_truthy(reason) else rule.description,
                rule.source,
                tuple(extra_inputs) if extra_inputs is not None else rule.inputs,
                rules.version,
                catalog_version,
                at,
            )
        )

    if lines is None:
        lines = effective_lines(bom) if js_truthy(bom) else ()
    by_role: dict[Any, Mapping] = {}
    for line in lines:
        by_role[prop(line, "role")] = line

    def find(role: str) -> Mapping | None:
        line = by_role.get(role)
        return line if js_truthy(line) else None

    item = _catalog_lookup(catalog)
    phase = _first_truthy(prop(bom, "phase"))
    arch = _first_truthy(prop(bom, "architecture"))

    # A. system architecture
    if not arch:
        add("PBC-A-001", [], "Project BOM does not declare a system architecture.", "Architecture determines the battery, protection and micro-inverter rules; it cannot be inferred.")

    # K. presence, resolvability, lifecycle
    k001 = rules.rule("PBC-K-001")
    exempt = set(k001.param("templateScopeExempt")) if template_scope else set()
    architecture_roles = k001.param("architectureRoles")
    required = [role for role in k001.param("requiredRoles") if role not in exempt]
    if arch:
        required += list(architecture_roles.get(arch, k001.param("otherArchitectureRoles"))) if isinstance(arch, str) else list(k001.param("otherArchitectureRoles"))
    for role in sorted(dict.fromkeys(required)):
        if not find(role):
            add("PBC-K-001", [], f"Required role {role} is missing from the project BOM.", f"{role} is mandatory for architecture {js_string(arch) if arch else 'UNDECLARED'}.")

    a002 = rules.rule("PBC-A-002")
    j001 = rules.rule("PBC-J-001")
    for line in lines:
        role_text = js_string(prop(line, "role"))
        component_id = prop(line, "componentId")
        if not js_truthy(component_id):
            add("PBC-K-002", [], f"Role {role_text} is present but no component is selected.")
            continue
        record = item(component_id)
        if record is None:
            add("PBC-K-003", [component_id], f'Component "{js_string(component_id)}" (role {role_text}) is not in the catalog.')
            continue
        status = js_string(record.get("status") if js_truthy(record.get("status")) else "ACTIVE").upper()
        if status != ComponentStatus.ACTIVE:
            add(
                "PBC-K-004",
                [component_id],
                f'Component "{js_string(prop(record, "name"))}" (role {role_text}) has catalog status {js_string(prop(record, "status"))}.',
                "A TEST or INACTIVE record must not be built (D11-D).",
            )
        quantity = js_finite_number(prop(line, "quantity"))
        if quantity is None or quantity <= 0:
            add(
                "PBC-L-001",
                [component_id],
                f"Role {role_text} has quantity {js_string(prop(line, 'quantity'))}.",
                "A role present in the BOM must carry a positive quantity. Use explicit removal instead of a zero quantity.",
            )
        record_phase = prop(record, "phase")
        if phase and js_truthy(record_phase) and j001.param("phaseAgnosticMarker") not in js_string(record_phase):
            if js_string(phase) not in js_string(record_phase):
                add(
                    "PBC-J-001",
                    [component_id],
                    f'Component "{js_string(prop(record, "name"))}" (role {role_text}) is {js_string(record_phase)} but the system is {js_string(phase)}.',
                )
        brand_value = prop(record, "brand")
        brand = js_string(brand_value if js_truthy(brand_value) else "").lower()
        if arch == "ENPHASE" and prop(line, "role") in a002.param("enphaseOnlyRoles") and brand and brand != "enphase":
            add(
                "PBC-A-002",
                [component_id],
                f'"{js_string(prop(record, "name"))}" is {js_string(brand_value)}; the Enphase architecture accepts only Enphase components in role {role_text}.',
                "Enphase architecture must not inherit Deye/SEG component rules (D12).",
            )
        if arch and arch != "ENPHASE" and brand == "enphase":
            add("PBC-A-002", [component_id], f'"{js_string(prop(record, "name"))}" is an Enphase component and cannot be used on a {js_string(arch)} architecture.')

    # P. AC isolator (D11)
    p001 = rules.rule("PBC-P-001")
    isolator = find(Role.AC_ISOLATOR)
    if isolator and js_truthy(prop(isolator, "componentId")) and phase:
        phase_key = phase if isinstance(phase, str) else js_string(phase)
        expected = p001.param("isolatorByPhase").get(phase_key, p001.param("defaultIsolator"))
        if not _same_value(prop(isolator, "componentId"), expected):
            add(
                "PBC-P-001",
                [prop(isolator, "componentId")],
                f'AC isolator is "{js_string(prop(isolator, "componentId"))}" but the approved {js_string(phase)} isolator is "{expected}".',
                p001.param("decisionByPhase").get(phase_key, p001.param("defaultDecision")),
            )
    prohibited = rules.rule("PBC-P-002").param("prohibitedRoles")
    for role in prohibited:
        if any(prop(line, "role") == role for line in lines):
            add("PBC-P-002", [], f"The BOM contains a {role} role.", f"D11-C: V1 does not model a separate {'DC isolator' if role == 'DC_ISOLATOR' else role}.")

    # Q. structure
    if not template_scope and not find(Role.STRUCTURE):
        add("PBC-Q-001", [], "No structure line is present in the project BOM.")

    # panel / inverter
    panel_line, inverter_line = find(Role.PANEL), find(Role.INVERTER)
    panel = item(prop(panel_line, "componentId")) if panel_line else None
    inverter = item(prop(inverter_line, "componentId")) if inverter_line else None

    # C. panel electrical limits
    if panel is not None:
        missing = [name for name in rules.rule("PBC-C-001").param("requiredFields") if js_finite_number(prop(panel, name)) is None]
        if missing:
            add(
                "PBC-C-001",
                [prop(panel, "id")],
                f'Panel "{js_string(prop(panel, "name"))}" is missing {", ".join(missing)}.',
                "String limits cannot be evaluated. No value is assumed.",
                missing,
            )

    # B / D. panel ↔ inverter
    if panel is not None and inverter is not None:
        max_in = js_finite_number(prop(inverter, "maxInputVoltage"))
        max_sys = js_finite_number(prop(panel, "maxSysVoltage"))
        if max_in is None:
            add(
                "PBC-D-001",
                [prop(inverter, "id")],
                f'Inverter "{js_string(prop(inverter, "name"))}" does not record maxInputVoltage.',
                "The DC input window cannot be evaluated. No limit is assumed.",
            )
        if max_in is not None and max_sys is not None and max_sys < max_in:
            add(
                "PBC-B-002",
                [prop(panel, "id"), prop(inverter, "id")],
                f"Panel maximum system voltage {number_text(max_sys)} V is below the inverter maximum input voltage {number_text(max_in)} V.",
            )
        voc, coefficient = js_finite_number(prop(panel, "voc")), js_finite_number(prop(panel, "tempCoeffVoc"))
        d002 = rules.rule("PBC-D-002")
        if voc is not None and coefficient is not None and max_in is not None:
            cold, stc = js_number(d002.param("minCellTemperatureC")), js_number(d002.param("stcTemperatureC"))
            with localcontext(EXACT):
                cold_voc = voc * (1 + (coefficient / 100) * (cold - stc))
            if cold_voc > max_in:
                add(
                    "PBC-D-002",
                    [prop(panel, "id"), prop(inverter, "id")],
                    f'A single "{js_string(prop(panel, "name"))}" reaches {to_fixed(cold_voc, 1)} V at {number_text(cold)} °C, above the inverter limit {number_text(max_in)} V.',
                    f"App.jsx stringConfig — Kerala minimum cell temperature {number_text(cold)} °C, STC {number_text(stc)} °C.",
                )
        add(
            "PBC-D-003",
            [prop(panel, "id"), prop(inverter, "id")],
            "String length is not declared in the project BOM.",
            "String voltage and current cannot be verified. No string configuration is assumed.",
        )
        mppt = prop(inverter, "mpptCount")
        strings = prop(inverter, "maxStringsPerMppt")
        add(
            "PBC-N-001",
            [prop(inverter, "id")],
            f"Inverter records mpptCount={js_string(coalesce(mppt, 'null'))} and maxStringsPerMppt={js_string(coalesce(strings, 'null'))}, but the BOM declares no string layout.",
            "Branch/circuit constraints are unverifiable in V1.",
        )
    elif panel is not None and inverter is None and arch != "ENPHASE":
        add("PBC-B-001", [prop(panel, "id")], "No inverter is selected; panel/inverter compatibility cannot be evaluated.")

    # O. DCDB / ACDB
    o001 = rules.rule("PBC-O-001")
    for role in o001.param("roles"):
        board_line = find(role)
        record = item(prop(board_line, "componentId")) if board_line else None
        record_phase = prop(record, "phase") if record is not None else None
        if record is not None and phase and js_truthy(record_phase):
            text = js_string(record_phase)
            if o001.param("phaseAgnosticMarker") not in text and js_string(phase) not in text:
                add("PBC-O-001", [prop(record, "id")], f'{role} "{js_string(prop(record, "name"))}" is {text} but the system is {js_string(phase)}.')

    # battery: E, F, G, H, I
    battery_line = find(Role.BATTERY)
    if battery_line and js_truthy(prop(battery_line, "componentId")):
        battery_id = prop(battery_line, "componentId")
        battery_item = item(battery_id)
        overlay = battery_master.get(battery_id) if isinstance(battery_id, str) else None
        battery = to_battery_master(battery_item, overlay or {}) if battery_item is not None else None
        if battery is not None:
            compat = check_battery_compatibility(
                battery,
                {
                    "inverter": inverter,
                    "sysType": _first_truthy(prop(bom, "sysType")) or "hybrid",
                    "phase": phase,
                    "quantity": js_finite_number(prop(battery_line, "quantity")) or Decimal(0),
                    "architecture": arch,
                },
            )
            by_id = {check["id"]: check for check in compat["checks"]}
            ids = [battery["componentId"]]

            def result_of(check_id: str) -> Any:
                return by_id[check_id]["result"] if check_id in by_id else None

            if result_of("BC-J") == CompatResult.FAIL:
                add("PBC-A-002", ids, by_id["BC-J"]["message"])
            if CompatResult.FAIL in (result_of("BC-A"), result_of("BC-B")):
                add("PBC-E-001", ids, " ".join(by_id[check]["message"] for check in ("BC-A", "BC-B") if result_of(check) == CompatResult.FAIL))
            if result_of("BC-C") == CompatResult.FAIL:
                add("PBC-F-001", ids, by_id["BC-C"]["message"])
            if result_of("BC-C") == CompatResult.INDETERMINATE:
                add("PBC-F-002", ids, by_id["BC-C"]["message"])
            if result_of("BC-D") == CompatResult.FAIL:
                add("PBC-G-001", ids, by_id["BC-D"]["message"])
            if result_of("BC-D") == CompatResult.INDETERMINATE:
                add("PBC-G-002", ids, by_id["BC-D"]["message"])
            if result_of("BC-E") == CompatResult.INDETERMINATE:
                add("PBC-I-001", ids, by_id["BC-E"]["message"])

            protection = resolve_protection_requirement(battery)
            protection_line = find(Role.BATTERY_PROTECTION)
            name = js_string(battery["displayName"])
            if is_nullish(battery["integratedProtection"]):
                add("PBC-H-001", ids, f'Protection mode for "{name}" is not recorded.', "Cannot safely validate this battery configuration.")
            if protection["mode"] == BatteryProtectionMode.EXTERNAL_REQUIRED:
                if is_nullish(protection["rating"]):
                    add(
                        "PBC-H-002",
                        ids,
                        f'External protection is required for "{name}" but no rating is recorded.',
                        "The rating must not be assumed, substituted, or derived from Ah (open item D12-E).",
                    )
                if not protection_line:
                    add("PBC-H-003", ids, f'External protection is required for "{name}" but no BATTERY_PROTECTION line is present.')
            if protection["mode"] == BatteryProtectionMode.INTEGRATED:
                if protection_line:
                    add(
                        "PBC-H-004",
                        [battery["componentId"], prop(protection_line, "componentId")],
                        f'"{name}" carries integrated protection but a generic BATTERY_PROTECTION line is present.',
                        "Prohibited on the Enphase architecture (D12).",
                    )
                cable_line = find(Role.BATTERY_CABLE)
                if cable_line:
                    add(
                        "PBC-H-004",
                        [battery["componentId"], prop(cable_line, "componentId")],
                        f'"{name}" carries integrated protection but a generic DC battery cable line is present.',
                        "Prohibited on the Enphase architecture (D12).",
                    )

    # M. Enphase premium quantities
    if arch == "ENPHASE":
        micro, panel_role = find(Role.MICRO_INVERTER), find(Role.PANEL)
        if micro and panel_role and js_finite_number(prop(micro, "quantity")) != js_finite_number(prop(panel_role, "quantity")):
            add(
                "PBC-M-001",
                [prop(micro, "componentId"), prop(panel_role, "componentId")],
                f"Micro-inverter quantity {js_string(prop(micro, 'quantity'))} does not equal the panel quantity {js_string(prop(panel_role, 'quantity'))}.",
                "D12 — one IQ8P per approved PV panel.",
            )
        battery_role = find(Role.BATTERY)
        m002 = rules.rule("PBC-M-002")
        if battery_role and js_finite_number(prop(battery_role, "quantity")) != js_number(m002.param("batteryQuantity")):
            expected = m002.param("batteryQuantity")
            add(
                "PBC-M-002",
                [prop(battery_role, "componentId")],
                f"Battery quantity is {js_string(prop(battery_role, 'quantity'))}.",
                (
                    "D12 — the current V1 Enphase package is one FlexPhase battery."
                    if js_number(expected) == 1
                    else f"D12 — the V1 Enphase package is {number_text(js_number(expected))} FlexPhase batteries."
                ),
            )
        m003 = rules.rule("PBC-M-003")
        system_type = _first_truthy(prop(bom, "sysType")) or m003.param("defaultSystemType")
        if system_type in m003.param("systemTypes") and not find(Role.ENERGY_SYSTEM_CONTROLLER):
            add("PBC-M-003", [], "No IQ System Controller line is present.", "D12 — the System Controller is mandatory for backup (hybrid only).")

    # R / S. upgrade
    if js_truthy(upgrade):
        source, target = js_finite_number(prop(upgrade, "fromKw")), js_finite_number(prop(upgrade, "toKw"))
        path = _find_path(rules.rule("PBC-R-001").param("approvedUpgradePaths"), source, target)
        if path is None:
            add("PBC-R-001", [], f"Upgrade {js_string(source)}→{js_string(target)} kW is not an engineering-approved path.")
        elif path["explicitBom"] is False:
            add("PBC-R-002", [], f"Upgrade path {path['id']} is approved but has no explicit BOM; it is auto-scaled.", path.get("note"))
    if js_truthy(future_upgrade):
        add("PBC-S-001", [], "Future-upgrade configuration is active.", "Panels size on panel kW while upsized components size on inverter kW. Verify each component.")

    ordered = tuple(sorted(findings, key=lambda finding: (finding.rule_id, ",".join(js_string(cid) for cid in finding.component_ids))))
    counts = Counts(
        blocked=sum(1 for finding in ordered if finding.severity == Severity.BLOCK),
        warning=sum(1 for finding in ordered if finding.severity == Severity.WARN),
        info=sum(1 for finding in ordered if finding.severity == Severity.INFO),
        checks_run=len(rules.rules),
    )
    status = CheckStatus.BLOCKED if counts.blocked else CheckStatus.WARNING if counts.warning else CheckStatus.VALID
    key = ";".join(f"{finding.rule_id}|{'+'.join(js_string(cid) for cid in finding.component_ids)}" for finding in ordered)
    return CheckResult(status, ordered, counts, rules.version, catalog_version, at, key)


# ---------------------------------------------------------------------------------------------------------------------
# validate-bom.mjs — battery consistency against the package profile (single implementation of the D1 detection)
# ---------------------------------------------------------------------------------------------------------------------

_BATTERY_NAME = re.compile(r"SEG\.|Okaya|Livguard|Flex Battery|LiFePO4|^New Battery", re.IGNORECASE | re.ASCII)
_MCCB_NAME = re.compile(r"MCCB", re.IGNORECASE | re.ASCII)
_BATTERY_CABLE_NAME = re.compile(r"Battery Cable", re.IGNORECASE | re.ASCII)
_CHANGE_OVER_NAME = re.compile(r"Change Over Switch\Z", re.IGNORECASE | re.ASCII)
_NON_NUMERIC = re.compile(r"[^\d.-]", re.ASCII)


def _loose_number(value: Any) -> Decimal:
    """``Number(String(v ?? '').replace(/[^\\d.-]/g, ''))`` when finite, else 0 (the validators' ``num``/``qn``)."""
    text = _NON_NUMERIC.sub("", "" if is_nullish(value) else js_string(value))
    number = js_number(text)
    return number if number.is_finite() else Decimal(0)


def _named(lines: Sequence[Mapping], pattern: re.Pattern) -> list[Mapping]:
    return [line for line in lines if pattern.search(js_string(prop(line, "name") if js_truthy(prop(line, "name")) else ""))]


def validate_battery_consistency(bom: Any, profile: Any, options: Mapping | None = None) -> Mapping:
    """``validateBatteryConsistency`` → ``{status, findings[{code, severity, message, expected, actual}]}``."""
    options = options or {}
    protection_mode = options.get("protectionMode", "UNKNOWN")
    protection_mode = "UNKNOWN" if protection_mode is UNDEFINED else protection_mode
    allow_orphans = options.get("allowAccessoriesWithoutBattery", False)
    require_mccb = options["requireMccbWhenBattery"] if options.get("requireMccbWhenBattery", UNDEFINED) is not UNDEFINED else protection_mode != "INTEGRATED"
    expect_dc_cable = protection_mode != "INTEGRATED"
    lines = js_array(_first_truthy(prop(bom, "bomLines"), prop(bom, "lines"))) or []
    findings: list[dict[str, Any]] = []

    def add(code: str, severity: str, message: str, expected: Any, actual: Any) -> None:
        findings.append({"code": code, "severity": severity, "message": message, "expected": expected, "actual": actual})

    want_qty = js_number(coalesce(prop(profile, "batteryQuantity"), 0))
    want_included = js_truthy(prop(profile, "batteryIncluded"))
    batteries, mccbs = _named(lines, _BATTERY_NAME), _named(lines, _MCCB_NAME)
    cables, change_overs = _named(lines, _BATTERY_CABLE_NAME), _named(lines, _CHANGE_OVER_NAME)
    with localcontext(EXACT):
        got_qty = sum((_loose_number(prop(line, "qty")) for line in batteries), Decimal(0))

    def name_of(line: Mapping) -> str:
        return js_string(prop(line, "name"))

    if not want_qty.is_nan() and want_qty > 0:  # Number('one') is NaN: NaN > 0 is false (a Decimal NaN comparison raises)
        if not batteries:
            add("BATTERY_MISSING", "BLOCKED", f"Profile declares batteryQuantity={number_text(want_qty)} but the BOM contains no battery line.", f"{number_text(want_qty)} battery item(s)", "none")
        elif got_qty != want_qty:
            add("BATTERY_QTY_MISMATCH", "BLOCKED", "Battery quantity does not match the profile.", want_qty, got_qty)
        if js_truthy(require_mccb) and not mccbs:
            add("BATTERY_MCCB_MISSING", "BLOCKED", "Battery is declared but no battery MCCB / protection line is present.", "at least 1 MCCB line", "none")
        if expect_dc_cable and not cables:
            add("BATTERY_CABLE_MISSING", "WARNING", "Battery is declared but no battery cable line is present.", "at least 1 battery cable line", "none")
    if want_qty == 0:
        if batteries:
            add(
                "BATTERY_UNEXPECTED",
                "BLOCKED",
                "Profile declares no battery but the BOM contains battery line(s).",
                "none",
                ", ".join(f"{name_of(line)} x{js_string(prop(line, 'qty'))}" for line in batteries),
            )
        if mccbs:
            add("BATTERY_MCCB_UNEXPECTED", "BLOCKED", "Profile declares no battery but a battery MCCB is present.", "none", ", ".join(name_of(line) for line in mccbs))
        if not js_truthy(allow_orphans) and (cables or change_overs):
            add("BATTERY_ACCESSORIES_ORPHANED", "WARNING", "Battery accessories present without a battery.", "none", ", ".join(name_of(line) for line in cables + change_overs))
    if protection_mode == "INTEGRATED" and cables:
        add(
            "BATTERY_DC_CABLE_PROHIBITED",
            "BLOCKED",
            "Generic DC battery cable present on an INTEGRATED-protection (Enphase) topology.",
            "none",
            ", ".join(name_of(line) for line in cables),
        )
    if protection_mode == "INTEGRATED" and mccbs:
        add("BATTERY_MCCB_PROHIBITED", "BLOCKED", "Generic battery MCCB present on an INTEGRATED-protection (Enphase) topology.", "none", ", ".join(name_of(line) for line in mccbs))
    if not batteries and (cables or change_overs):
        add(
            "BATTERY_ACCESSORIES_WITHOUT_BATTERY",
            "BLOCKED",
            "BOM contains battery cabling and/or a change-over switch but no battery. The system as specified cannot be installed.",
            "battery present, or accessories absent",
            ", ".join(f"{name_of(line)} x{js_string(prop(line, 'qty'))}" for line in cables + change_overs),
        )
    if want_included and want_qty == 0:
        add("PROFILE_INCONSISTENT", "WARNING", "Profile sets batteryIncluded=true but batteryQuantity=0.", "quantity > 0", 0)
    status = "BLOCKED" if any(item["severity"] == "BLOCKED" for item in findings) else "WARNING" if any(item["severity"] == "WARNING" for item in findings) else "VALID"
    return deep_freeze({"status": status, "findings": findings})


# ---------------------------------------------------------------------------------------------------------------------
# engineeringValidation.js — "is this configuration technically valid?" (the configuration rule set, ENG-*)
# ---------------------------------------------------------------------------------------------------------------------

VALIDATION_RULES_VERSION = "phase1e.eng"

_ENG_RULES = (
    _rule(
        "ENG-BAT-001",
        "BAT",
        "BLOCKED",
        "Battery declared by the package but absent from the BOM",
        ["profile.batteryQuantity", "bom.lines"],
        "packageProfiles + defect D1",
    ),
    _rule("ENG-BAT-002", "BAT", "BLOCKED", "Battery quantity differs from the package profile", ["profile.batteryQuantity", "bom.lines"], "packageProfiles"),
    _rule("ENG-BAT-003", "BAT", "BLOCKED", "Battery present without its MCCB protection", ["bom.lines"], "bomTemplates.hybrid.slots[4] mccb_box (batQty 1:1)"),
    _rule("ENG-BAT-004", "BAT", "BLOCKED", "Battery accessories present without a battery", ["bom.lines"], "defect D1"),
    _rule("ENG-BAT-005", "BAT", "BLOCKED", "Battery present when the package declares none", ["profile.batteryQuantity", "bom.lines"], "packageProfiles"),
    _rule("ENG-BAT-006", "BAT", "WARNING", "Profile internally inconsistent (batteryIncluded vs batteryQuantity)", ["profile"], "packageProfiles"),
    _rule(
        "ENG-SYS-001",
        "SYS",
        "BLOCKED",
        "System size not offered for this system type",
        ["sysType", "size", "catalog.bomTemplates"],
        "bomTemplates[sysType].sizes",
    ),
    _rule(
        "ENG-SYS-002",
        "SYS",
        "BLOCKED",
        "Phase not supported for this size",
        ["size", "phase", "catalog.bomTemplates"],
        "bomTemplates[sysType].threePhase",
        threePhase="3P",
    ),
    _rule(
        "ENG-SYS-003",
        "SYS",
        "BLOCKED",
        "Mandatory BOM component missing (N/A line)",
        ["bom.lines"],
        "App.jsx pushes an N/A ₹0 line when no candidate resolves",
    ),
    _rule("ENG-CMP-001", "CMP", "BLOCKED", "A selected component is not ACTIVE", ["bom.lines", "catalog"], "Phase 1G catalog lifecycle"),
    _rule(
        "ENG-CMP-002",
        "CMP",
        "BLOCKED",
        "Inverter phase does not match system phase",
        ["bom.lines", "phase"],
        "App.jsx phaseFilter()",
        phaseAgnosticMarker="HYB",
    ),
    _rule("ENG-CMP-003", "CMP", "BLOCKED", "Inverter type incompatible with system type", ["bom.lines", "sysType"], "App.jsx phaseFilter() filterType"),
    _rule(
        "ENG-CMP-004",
        "CMP",
        "BLOCKED",
        "Duplicate incompatible component in the same role",
        ["bom.lines"],
        "one slot per role in bomTemplates",
        singleLineCategories=["inverter", "battery", "dcdb", "acdb"],
    ),
    _rule(
        "ENG-CMP-005",
        "CMP",
        "WARNING",
        "Component chosen by fallback rather than an approved selection",
        ["bom.lines.resolution"],
        "Phase 0 hotfix H3",
    ),
    _rule(
        "ENG-PNL-001",
        "PNL",
        "BLOCKED",
        "Subsidy requires DCR panels but a non-DCR panel is selected",
        ["subsidyType", "bom.lines"],
        "App.jsx dcrFilter() — subsidy forces DCR",
        subsidyTypes=["residential", "ghs"],
    ),
    _rule(
        "ENG-PNL-002",
        "PNL",
        "WARNING",
        "Installed DC capacity deviates materially from the nameplate system size",
        ["size", "panel.watt", "panel.qty"],
        "App.jsx panel qty = ceil(size*1000/watt)",
    ),
    _rule(
        "ENG-STR-001",
        "STR",
        "WARNING",
        "String open-circuit voltage at minimum cell temperature approaches the inverter limit",
        ["panel.voc", "panel.tempCoeffVoc", "inverter.maxInputVoltage"],
        "App.jsx stringConfig — Kerala cell temp min 15°C, STC 25°C",
    ),
    _rule(
        "ENG-BAT-007",
        "BAT",
        "WARNING",
        "Battery protection mode not confirmed by Engineering",
        ["battery.componentId"],
        "decision D12 — per-battery confirmation pending",
    ),
    _rule(
        "ENG-BAT-008",
        "BAT",
        "BLOCKED",
        "Generic Deye/SEG battery protection or DC cable used on an Enphase topology",
        ["bom.lines", "battery.protection.mode"],
        "decision D12 — prohibited on premium",
    ),
    _rule(
        "ENG-BAT-009",
        "BAT",
        "BLOCKED",
        "Selected battery is not engineering-approved",
        ["battery.approvalStatus"],
        "Phase 1D review correction — C50",
    ),
    _rule(
        "ENG-BAT-010",
        "BAT",
        "BLOCKED",
        "Battery data required to issue the BOM safely is not recorded",
        ["battery.missingForSafeIssue", "battery.protection.rating"],
        "Phase 1D — C44 / C51",
    ),
    _rule(
        "ENG-ENP-001",
        "ENP",
        "BLOCKED",
        "Premium hybrid requires an IQ System Controller",
        ["bom.lines", "tier", "sysType"],
        "decision D12 — ENERGY_SYSTEM_CONTROLLER required",
    ),
    _rule(
        "ENG-ENP-002",
        "ENP",
        "BLOCKED",
        "IQ8P micro-inverter quantity must equal approved panel quantity",
        ["bom.lines"],
        "decision D12 — IQ8P qty = panel qty",
    ),
    _rule(
        "ENG-ENP-003",
        "ENP",
        "WARNING",
        "Enphase component configuration not yet confirmed",
        ["bom.lines"],
        "decision D12 — PENDING_CONFIGURATION_CONFIRMATION",
        pendingComponents={"en3": "Envoy S Metered", "en4": "IQ Relay", "en6": "CT 200"},
    ),
    _rule(
        "ENG-ENP-004",
        "ENP",
        "WARNING",
        "Enphase communication cable identity not yet in the catalog",
        ["tier", "sysType"],
        "decision D12 — pending catalog identity",
    ),
    _rule(
        "ENG-UPG-001",
        "UPG",
        "BLOCKED",
        "Upgrade path is not engineering-approved",
        ["fromKw", "toKw"],
        "Build Spec §8 + decision C10",
        approvedUpgradePaths=APPROVED_UPGRADE_PATHS,
    ),
    _rule(
        "ENG-UPG-002",
        "UPG",
        "WARNING",
        "Approved upgrade path has no explicit BOM definition and is auto-scaled",
        ["fromKw", "toKw"],
        "bomTemplates.upgrade.fixedItems path keys",
    ),
    _rule(
        "ENG-UPG-003",
        "UPG",
        "BLOCKED",
        "Mandatory upgrade sections not selected — quotation not sellable",
        ["upgradeSections"],
        "defect D10 + decision C24",
        mandatorySections=list(MANDATORY_UPGRADE_SECTIONS),
    ),
    _rule(
        "ENG-DEV-001",
        "DEV",
        "BLOCKED",
        "Panel allocation incomplete — one or more panels cannot be validly assigned to a micro-inverter / optimizer device (locked 2.10). "
        "Every panel must be explicitly covered; a silent remainder is prohibited.",
        ["bom.engineering.blockers", "catalog.categories.enphase|hoymiles.items[].{deviceType,panelsPerDevice}"],
        "Phase C.1 deviceAllocation.js + locked 2.10",
    ),
    _rule(
        "ENG-SYS-004",
        "SYS",
        "BLOCKED",
        "Unsupported system configuration — the requested (systemType, size, phase) combination is not covered by any registered package. " "Silent coercion to a supported combination is prohibited.",
        ["systemType", "size", "phase"],
        "Phase C.5 + locked §6",
    ),
)

#: ``engineeringValidation.js`` RULES as data (the JavaScript carries no version; ``phase1e.eng`` names this register).
VALIDATION_RULE_SET = RuleSet("engineeringValidation", VALIDATION_RULES_VERSION, _ENG_RULES)

_REGISTRIES["engineeringValidation"] = _Registry(
    VALIDATION_RULE_SET,
    {
        "ENG-SYS-002": {"threePhase": "text"},
        "ENG-CMP-002": {"phaseAgnosticMarker": "text"},
        "ENG-CMP-004": {"singleLineCategories": "texts"},
        "ENG-PNL-001": {"subsidyTypes": "texts"},
        "ENG-ENP-003": {"pendingComponents": "textMap"},
        "ENG-UPG-001": {"approvedUpgradePaths": "upgradePaths"},
        "ENG-UPG-003": {"mandatorySections": "texts"},
    },
)

#: validate-bom.mjs finding code → ENG rule (unmapped codes fall back to ENG-BAT-006, as in the JavaScript).
_BATTERY_CODE_TO_RULE: Mapping[str, str] = FrozenDict(
    BATTERY_MISSING="ENG-BAT-001",
    BATTERY_QTY_MISMATCH="ENG-BAT-002",
    BATTERY_MCCB_MISSING="ENG-BAT-003",
    BATTERY_ACCESSORIES_WITHOUT_BATTERY="ENG-BAT-004",
    BATTERY_UNEXPECTED="ENG-BAT-005",
    BATTERY_MCCB_UNEXPECTED="ENG-BAT-005",
    BATTERY_ACCESSORIES_ORPHANED="ENG-BAT-004",
    PROFILE_INCONSISTENT="ENG-BAT-006",
    BATTERY_CABLE_MISSING="ENG-BAT-006",
    BATTERY_DC_CABLE_PROHIBITED="ENG-BAT-008",
    BATTERY_MCCB_PROHIBITED="ENG-BAT-008",
)
_IQ8P = re.compile(r"IQ8P", re.IGNORECASE | re.ASCII)
_SYSTEM_CONTROLLER = re.compile(r"System Controller", re.IGNORECASE | re.ASCII)
_BATTERY_CABLE_25 = re.compile(r"25mm Battery Cable", re.IGNORECASE | re.ASCII)


@dataclass(frozen=True)
class ValidationFinding:
    """``{ruleId, severity, description, message, detail, source}`` (``detail`` omitted when the rule gives none)."""

    rule_id: str
    severity: Severity
    description: str
    message: str
    detail: Any
    source: str

    def as_dict(self) -> dict[str, Any]:
        out = {"ruleId": self.rule_id, "severity": self.severity.legacy, "description": self.description, "message": self.message, "detail": self.detail, "source": self.source}
        if self.detail is UNDEFINED:
            del out["detail"]
        return out


@dataclass(frozen=True)
class ValidationResult:
    status: CheckStatus
    findings: tuple[ValidationFinding, ...]
    blocked: int
    warning: int
    rules_evaluated: int

    def as_dict(self) -> dict[str, Any]:
        return {
            "status": self.status.value,
            "findings": [finding.as_dict() for finding in self.findings],
            "counts": {"blocked": self.blocked, "warning": self.warning},
            "rulesEvaluated": self.rules_evaluated,
        }


def validate_engineering(configuration: Mapping | None = None, *, rule_set: RuleSet | None = None) -> ValidationResult:
    """``validateEngineering({bom, profile, catalog, sysType, size, tier, phase, subsidyType, upgrade, battery,
    batteryProtectionMode})`` — the configuration-time rule set (ENG-*)."""
    rules = rule_set or VALIDATION_RULE_SET
    if rules.engine != "engineeringValidation":
        raise RuleSetError("validate_engineering needs an engineeringValidation rule set.")
    config = configuration or {}
    bom, profile, catalog = config.get("bom"), config.get("profile"), config.get("catalog")
    sys_type = config.get("sysType", "ongrid")
    sys_type = "ongrid" if sys_type is UNDEFINED else sys_type
    size, tier, phase = config.get("size", UNDEFINED), config.get("tier"), config.get("phase")
    subsidy_type, upgrade = config.get("subsidyType"), config.get("upgrade")
    lines = js_array(_first_truthy(prop(bom, "bomLines"), prop(bom, "lines"))) or []
    findings: list[ValidationFinding] = []

    def add(rule_id: str, message: str, detail: Any = UNDEFINED) -> None:
        rule = rules.rule(rule_id)
        findings.append(ValidationFinding(rule_id, rule.severity, rule.description, message, detail, rule.source))

    def name_of(line: Any) -> str:
        name = prop(line, "name")
        return js_string(name) if js_truthy(name) else ""

    protection_mode = config.get("batteryProtectionMode") or "UNKNOWN"
    if js_truthy(profile):
        consistency = validate_battery_consistency({"bomLines": lines}, profile, {"protectionMode": protection_mode})
        for item in consistency["findings"]:
            add(_BATTERY_CODE_TO_RULE.get(item["code"], "ENG-BAT-006"), item["message"], {"expected": item["expected"], "actual": item["actual"], "code": item["code"]})

    template = prop(prop(catalog, "bomTemplates"), sys_type) if isinstance(sys_type, str) else UNDEFINED
    if js_truthy(template) and not is_nullish(size):
        sizes = js_keys(prop(template, "sizes") or {})
        size_text = js_string(size)
        if sizes and size_text not in sizes:
            add("ENG-SYS-001", f'Size "{size_text}" is not offered for system type "{js_string(sys_type)}". Available: {", ".join(sizes)}.')
        three_phase = size_text in [js_string(value) for value in js_array(prop(template, "threePhase")) or []]
        if phase == rules.rule("ENG-SYS-002").param("threePhase") and not three_phase and size_text in sizes:
            add("ENG-SYS-002", f'Size "{size_text}" is not listed as three-phase for "{js_string(sys_type)}".')

    for line in lines:
        if js_truthy(prop(line, "isNA")) or (not name_of(line) and _loose_number(prop(line, "qty")) > 0) or name_of(line) == "N/A":
            label = _first_truthy(prop(line, "label"), prop(line, "category")) or "unknown slot"
            add("ENG-SYS-003", f'No component resolved for "{js_string(label)}" — the line is priced at zero.', {"pos": prop(line, "pos"), "category": prop(line, "category")})

    if js_truthy(catalog):
        for line in lines:
            component_id = _first_truthy(prop(line, "itemId"), prop(line, "catalogItemId"))
            if not component_id:
                continue
            category_key, raw = _find_raw(catalog, component_id)
            status = prop(raw, "status") if js_truthy(prop(raw, "status")) else classify_catalog_item(category_key, raw)["status"]
            if status != ComponentStatus.ACTIVE:
                add(
                    "ENG-CMP-001",
                    f'Component "{name_of(line) or js_string(component_id)}" is marked {js_string(status)} and must not be used in a production configuration.',
                    {"componentId": component_id, "status": status},
                )

    inverter = next((line for line in lines if prop(line, "category") == "inverter"), None)
    inverter_phase = prop(prop(inverter, "raw"), "phase")
    marker = rules.rule("ENG-CMP-002").param("phaseAgnosticMarker")
    if inverter is not None and js_truthy(phase) and js_truthy(inverter_phase) and inverter_phase != marker and inverter_phase != phase:
        add("ENG-CMP-002", f"Inverter phase {js_string(inverter_phase)} does not match system phase {js_string(phase)}.")
    if inverter is not None and sys_type == "hybrid" and js_truthy(prop(inverter, "type")) and prop(inverter, "type") != "hybrid":
        add("ENG-CMP-003", f'Hybrid system has an inverter of type "{js_string(prop(inverter, "type"))}".')

    role_count: dict[str, int] = {}
    single = rules.rule("ENG-CMP-004").param("singleLineCategories")
    for line in lines:
        category = prop(line, "category")
        if js_truthy(category) and category in single:
            role_count[category] = role_count.get(category, 0) + 1
    for role, count in role_count.items():
        if count > 1:
            add("ENG-CMP-004", f'{count} separate "{role}" lines in one BOM.', {"role": role, "count": count})

    fallbacks = [line for line in lines if js_truthy(prop(line, "autoSelected"))]
    if fallbacks:
        add(
            "ENG-CMP-005",
            f"{len(fallbacks)} component(s) chosen by fallback rather than an approved selection.",
            {"components": [{"category": prop(line, "category"), "name": name_of(line), "resolution": prop(line, "resolution")} for line in fallbacks]},
        )

    if subsidy_type in rules.rule("ENG-PNL-001").param("subsidyTypes"):
        panel = next((line for line in lines if prop(line, "category") == "panel"), None)
        panel_type = prop(panel, "panelType")
        panel_type = panel_type if js_truthy(panel_type) else ("DCR" if js_truthy(prop(panel, "dcr")) else None)
        if panel is not None and js_truthy(panel_type) and panel_type != "DCR":
            add("ENG-PNL-001", f'Subsidy "{js_string(subsidy_type)}" requires DCR panels but "{name_of(panel)}" is {js_string(panel_type)}.')

    if tier == "premium":

        def with_id(component_id: str) -> list:
            return [line for line in lines if _first_truthy(prop(line, "itemId"), prop(line, "catalogItemId")) == component_id]

        iq8p = [line for line in lines if _IQ8P.search(name_of(line))]
        panel = next((line for line in lines if prop(line, "category") == "panel"), None)
        if iq8p and panel is not None:
            iq_qty, panel_qty = _loose_number(prop(iq8p[0], "qty")), _loose_number(prop(panel, "qty"))
            if iq_qty != panel_qty:
                add("ENG-ENP-002", f"IQ8P quantity {number_text(iq_qty)} does not equal panel quantity {number_text(panel_qty)}.", {"iqQty": iq_qty, "pQty": panel_qty})
        if sys_type == "hybrid":
            if not any(_SYSTEM_CONTROLLER.search(name_of(line)) for line in lines):
                add("ENG-ENP-001", "Premium hybrid backup architecture requires an IQ System Controller; none is present.")
            if with_id("mb1"):
                add("ENG-BAT-008", "Generic 160A MCCB (mb1) is present on an Enphase premium system. Prohibited by D12.")
            if any(_BATTERY_CABLE_25.search(name_of(line)) for line in lines):
                add("ENG-BAT-008", "Generic 25 mm DC battery cable is present on an Enphase premium system. Prohibited by D12.")
            add(
                "ENG-ENP-004",
                "Enphase communication/control cable (supplied commercial BOM: 10 m, ₹6,000 + GST) has no catalog identity yet. Not added to the BOM.",
            )
        pending = rules.rule("ENG-ENP-003").param("pendingComponents")
        present = [component_id for component_id in pending if with_id(component_id)]
        if present:
            add(
                "ENG-ENP-003",
                f"Configuration not confirmed for: {', '.join(pending[component_id] for component_id in present)}. "
                "These functions may be integrated into the IQ System Controller. Retained pending confirmation (D12).",
                {"componentIds": present},
            )

    if protection_mode == "UNKNOWN" and any(prop(line, "category") == "battery" for line in lines):
        add("ENG-BAT-007", "Battery protection mode is not confirmed by Engineering. Existing template behaviour preserved (D12).")

    battery = config.get("battery") or None
    if js_truthy(prop(battery, "required")):
        component_name = prop(prop(battery, "component"), "name")
        if prop(battery, "engineeringApproved") is False:
            label = component_name if js_truthy(component_name) else prop(battery, "componentId")
            add(
                "ENG-BAT-009",
                f'Battery "{js_string(label)}" is {js_string(prop(battery, "approvalStatus"))}. '
                "The battery is retained in the BOM — it is not removed — but the configuration cannot be issued until Engineering approves it.",
                {"componentId": prop(battery, "componentId"), "approvalStatus": prop(battery, "approvalStatus")},
            )
        missing = [js_string(name) for name in js_array(prop(battery, "missingForSafeIssue")) or []]
        protection = prop(battery, "protection")
        if prop(protection, "mode") == "EXTERNAL_REQUIRED" and is_nullish(prop(protection, "rating")):
            missing.append("protectionRating")
        if missing:
            unique = list(dict.fromkeys(missing))
            add(
                "ENG-BAT-010",
                f"Cannot safely validate this battery configuration — {', '.join(unique)} not recorded. " "The value must not be assumed, substituted, or derived from Ah (open item D12-E).",
                {"componentId": prop(battery, "componentId"), "missing": unique},
            )

    if js_truthy(upgrade):
        from_kw, to_kw, sections = prop(upgrade, "fromKw"), prop(upgrade, "toKw"), prop(upgrade, "sections")
        source, target = js_number(from_kw), js_number(to_kw)
        path = _find_path(rules.rule("ENG-UPG-001").param("approvedUpgradePaths"), source if source.is_finite() else None, target if target.is_finite() else None)
        if path is None:
            add("ENG-UPG-001", f"Upgrade {js_string(from_kw)}→{js_string(to_kw)} kW is not an engineering-approved path.")
        elif not path["explicitBom"]:
            add(
                "ENG-UPG-002",
                f"Upgrade path {path['id']} has no explicit BOM definition; the BOM is auto-scaled from the nearest known path.",
                {"pathId": path["id"]},
            )
        if js_truthy(sections):
            policy = check_upgrade_section_policy(sections, rules.rule("ENG-UPG-003").param("mandatorySections"))
            if not policy["sellable"]:
                add("ENG-UPG-003", policy["message"], {"missing": policy["missingMandatory"]})

    frozen = tuple(ValidationFinding(f.rule_id, f.severity, f.description, f.message, deep_freeze(f.detail) if f.detail is not UNDEFINED else UNDEFINED, f.source) for f in findings)
    blocked = sum(1 for finding in frozen if finding.severity == Severity.BLOCK)
    warning = sum(1 for finding in frozen if finding.severity == Severity.WARN)
    status = CheckStatus.BLOCKED if blocked else CheckStatus.WARNING if warning else CheckStatus.VALID
    return ValidationResult(status, frozen, blocked, warning, len(rules.rules))


def _find_raw(catalog: Any, component_id: Any) -> tuple[Any, Any]:
    """``findRaw``: ``(categoryKey, item)`` of the first category (catalogue order) holding the id, else undefined."""
    categories = prop(catalog, "categories")
    for key in js_keys(categories):
        for candidate in js_array(prop(categories[key], "items")) or []:
            if _same_value(prop(candidate, "id"), component_id):
                return key, candidate
    return UNDEFINED, UNDEFINED
