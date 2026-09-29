"""Subsidy engine: PM Surya Ghar central financial assistance (CFA) eligibility and amount.

Port of Flarize ``src/lib/subsidyEngine.js`` (version ``'1.0.0'``, scheme ``PMSG_2024_V1``, spec §4), a protected
formula (spec §19). Evaluation order, exactly as the JavaScript:

1. no subsidy type (``None``/``''``/``'none'``) → ``NO_SUBSIDY``; 2. no configuration → ``MISSING_CONFIG``;
3. size not a positive number → ``INVALID_INPUT``; 4. connection not ``'domestic'`` → ``INELIGIBLE_CONNECTION``;
5. DCR required and the panels are not DCR (residential or GHS) → ``GIVE_IT_UP`` (subsidy forfeited, net metering
   kept); 6. residential: 1–10 kW, banded ``min(remaining, band) × rate`` over the tiers (₹30,000/kW to 2 kW,
   ₹18,000/kW for the 3rd kW), capped at ₹78,000, rounded; 7. GHS/RWA: ``min(kW, 3) × houses`` capped at 500 kW,
   × ₹18,000; 8. anything else → ``INVALID_INPUT``.

Rates, bands and caps are configuration (``subsidy-config.json`` → ``pricing_cost_config`` ``subsidy.*``); the
JavaScript fallbacks for missing keys are kept. ``calculated_at`` is supplied by the caller (engines never read the
clock).
"""

from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal
from enum import StrEnum
from typing import Any, Mapping

from engines.money import ZERO, exact, is_number, js_round, js_text, json_number

SUBSIDY_ENGINE_VERSION = "1.0.0"
SUBSIDY_SOURCE = "SUBSIDY_ENGINE"
SUBSIDY_STATUS_LIVE = "LIVE"
DEFAULT_SCHEME_NAME = "PM Surya Ghar: Muft Bijli Yojana"
DEFAULT_SCHEME_VERSION = "PMSG_2024_V1"

#: JavaScript fallbacks for keys missing from the configuration.
DEFAULT_MAX_CAPACITY_KW = Decimal(10)
DEFAULT_MIN_CAPACITY_KW = Decimal(1)
DEFAULT_MAX_SUBSIDY = Decimal(78000)
DEFAULT_GHS_RATE_PER_KW = Decimal(18000)
DEFAULT_GHS_MAX_KW_PER_HOUSE = Decimal(3)
DEFAULT_GHS_MAX_COMMUNITY_KW = Decimal(500)


class SubsidyError(StrEnum):
    INVALID_INPUT = "INVALID_INPUT"
    INELIGIBLE_CAPACITY = "INELIGIBLE_CAPACITY"
    INELIGIBLE_CONNECTION = "INELIGIBLE_CONNECTION"
    INELIGIBLE_DCR = "INELIGIBLE_DCR"
    MISSING_CONFIG = "MISSING_CONFIG"
    GIVE_IT_UP = "GIVE_IT_UP"
    NO_SUBSIDY = "NO_SUBSIDY"


class EligibilityStatus(StrEnum):
    ELIGIBLE = "ELIGIBLE"
    INELIGIBLE = "INELIGIBLE"
    GIVE_IT_UP = "GIVE_IT_UP"


class SubsidyType(StrEnum):
    RESIDENTIAL = "residential"
    GHS = "ghs"
    NONE = "none"


# ---- configuration ------------------------------------------------------------------------------------------------


@dataclass(frozen=True)
class SubsidyTier:
    up_to_kw: Decimal
    rate_per_kw: Decimal


@dataclass(frozen=True)
class ResidentialRule:
    tiers: tuple[SubsidyTier, ...] = ()
    max_subsidy: Decimal | None = None
    max_capacity_kw: Decimal | None = None
    min_capacity_kw: Decimal | None = None


@dataclass(frozen=True)
class GhsRule:
    rate_per_kw: Decimal | None = None
    max_kw_per_house: Decimal | None = None
    max_community_kw: Decimal | None = None


@dataclass(frozen=True)
class SchemeInfo:
    name: str | None = None
    scheme_version: str | None = None
    effective_period: str | None = None


@dataclass(frozen=True)
class SubsidyConfig:
    """``subsidy-config.json``. A missing section is ``None`` (``MISSING_CONFIG`` when that type is asked for).

    ``dcr_required`` follows ``config.dcr?.required !== false``: only an explicit ``False`` waives DCR.
    """

    config_version: str | None = None
    scheme: SchemeInfo | None = None
    residential: ResidentialRule | None = None
    ghs: GhsRule | None = None
    state_top_up_kerala: Decimal | None = None
    dcr_required: bool = True

    @classmethod
    def from_json(cls, data: Mapping[str, Any]) -> SubsidyConfig:
        scheme = data.get("scheme")
        residential = data.get("residential")
        ghs = data.get("ghs")
        top_up = data.get("stateTopUp")
        dcr = data.get("dcr")
        return cls(
            config_version=data.get("configVersion"),
            scheme=SchemeInfo(scheme.get("name"), scheme.get("schemeVersion"), scheme.get("effectivePeriod")) if isinstance(scheme, Mapping) else None,
            residential=(
                ResidentialRule(
                    tiers=tuple(SubsidyTier(json_number(tier.get("upToKw")), json_number(tier.get("ratePerKw"))) for tier in residential.get("tiers") or () if isinstance(tier, Mapping)),
                    max_subsidy=json_number(residential.get("maxSubsidy")),
                    max_capacity_kw=json_number(residential.get("maxCapacityKw")),
                    min_capacity_kw=json_number(residential.get("minCapacityKw")),
                )
                if isinstance(residential, Mapping)
                else None
            ),
            ghs=(GhsRule(json_number(ghs.get("ratePerKw")), json_number(ghs.get("maxKwPerHouse")), json_number(ghs.get("maxCommunityKw"))) if isinstance(ghs, Mapping) else None),
            state_top_up_kerala=json_number(top_up.get("kerala")) if isinstance(top_up, Mapping) else None,
            dcr_required=not (isinstance(dcr, Mapping) and dcr.get("required") is False),
        )


def validate_subsidy_config(data: Any) -> tuple[bool, list[str]]:
    """``validateSubsidyConfig`` on the raw JSON: ``(valid, errors)``."""
    if not isinstance(data, Mapping):
        return False, ["Config must be a non-null object"]
    errors = []

    def positive(section: Mapping[str, Any], key: str) -> bool:
        value = section.get(key)
        return value is not None and not (isinstance(value, (int, float, Decimal)) and not isinstance(value, bool) and value <= 0)

    residential = data.get("residential")
    if not residential:
        errors.append("Missing residential section")
    else:
        if not isinstance(residential.get("tiers"), list) or not residential.get("tiers"):
            errors.append("residential.tiers must be a non-empty array")
        if not positive(residential, "maxSubsidy"):
            errors.append("residential.maxSubsidy must be a positive number")
        if not positive(residential, "maxCapacityKw"):
            errors.append("residential.maxCapacityKw must be a positive number")
    ghs = data.get("ghs")
    if not ghs:
        errors.append("Missing GHS section")
    else:
        for key in ("ratePerKw", "maxKwPerHouse", "maxCommunityKw"):
            if not positive(ghs, key):
                errors.append(f"ghs.{key} must be a positive number")
    if not data.get("scheme"):
        errors.append("Missing scheme section")
    return not errors, errors


# ---- results -------------------------------------------------------------------------------------------------------


def _scheme(config: SubsidyConfig | None) -> tuple[str, str]:
    scheme = config.scheme if config is not None and config.scheme is not None else SchemeInfo()
    return (scheme.name if scheme.name is not None else DEFAULT_SCHEME_NAME, scheme.scheme_version if scheme.scheme_version is not None else DEFAULT_SCHEME_VERSION)


@dataclass(frozen=True)
class SubsidyEligible:
    """An eligible residential or GHS result."""

    subsidy_type: str
    eligible_capacity_kw: Decimal
    central_subsidy: Decimal
    state_subsidy: Decimal
    total_subsidy: Decimal
    dcr_required: bool
    scheme_name: str
    scheme_version: str
    effective_period: str | None
    calculated_at: str | None
    ghs_houses: Decimal | int | None = None
    ghs_total_eligible_kw: Decimal | None = None
    available: bool = True
    status: str = SUBSIDY_STATUS_LIVE
    eligibility_status: EligibilityStatus = EligibilityStatus.ELIGIBLE

    def as_dict(self) -> dict[str, Any]:
        result: dict[str, Any] = {
            "source": SUBSIDY_SOURCE,
            "status": SUBSIDY_STATUS_LIVE,
            "available": True,
            "engineVersion": SUBSIDY_ENGINE_VERSION,
            "treatment": "ENGINE_RESULT",
            "subsidyType": self.subsidy_type,
            "eligibleCapacityKw": self.eligible_capacity_kw,
            "centralSubsidy": self.central_subsidy,
            "stateSubsidy": self.state_subsidy,
            "totalSubsidy": self.total_subsidy,
            "subsidyAmount": self.total_subsidy,
            "eligibilityStatus": str(EligibilityStatus.ELIGIBLE),
            "eligibilityReason": None,
            "dcrRequired": self.dcr_required,
        }
        if self.subsidy_type == SubsidyType.GHS:
            result["ghsHouses"] = self.ghs_houses
            result["ghsTotalEligibleKw"] = self.ghs_total_eligible_kw
        result.update(
            {
                "schemeName": self.scheme_name,
                "schemeVersion": self.scheme_version,
                "calculatedAt": self.calculated_at,
                "postSubsidyInvestment": None,
                "assumptions": {
                    "subsidyBasis": "CENTRAL_CFA_ONLY — no Kerala state top-up",
                    "stateTopUp": self.state_subsidy,
                    "dcrRequirement": self.dcr_required,
                    "effectivePeriod": self.effective_period,
                },
            }
        )
        return result


@dataclass(frozen=True)
class SubsidyIneligible:
    """``INELIGIBLE_CONNECTION`` / ``INELIGIBLE_CAPACITY``: amounts zero, the reason stated."""

    status: SubsidyError
    reason: str
    subsidy_type: str | None
    dcr_required: bool
    scheme_name: str
    scheme_version: str
    calculated_at: str | None
    available: bool = False
    total_subsidy: Decimal = ZERO
    eligibility_status: EligibilityStatus = EligibilityStatus.INELIGIBLE

    def as_dict(self) -> dict[str, Any]:
        return {
            "source": SUBSIDY_SOURCE,
            "status": str(self.status),
            "available": False,
            "reason": self.reason,
            "engineVersion": SUBSIDY_ENGINE_VERSION,
            "subsidyType": self.subsidy_type,
            "eligibilityStatus": str(EligibilityStatus.INELIGIBLE),
            "eligibilityReason": self.reason,
            "centralSubsidy": ZERO,
            "stateSubsidy": ZERO,
            "totalSubsidy": ZERO,
            "subsidyAmount": ZERO,
            "dcrRequired": self.dcr_required,
            "schemeName": self.scheme_name,
            "schemeVersion": self.scheme_version,
            "calculatedAt": self.calculated_at,
            "postSubsidyInvestment": None,
        }


GIVE_IT_UP_REASON = 'Non-DCR panels selected. Subsidy forfeited under "Give It Up" provision. Net metering benefits retained.'


@dataclass(frozen=True)
class SubsidyGivenUp:
    """``GIVE_IT_UP``: non-DCR panels, subsidy forfeited, DCR waived, net metering retained."""

    subsidy_type: str
    eligible_capacity_kw: Decimal | int
    scheme_name: str
    scheme_version: str
    calculated_at: str | None
    reason: str = GIVE_IT_UP_REASON
    available: bool = False
    status: SubsidyError = SubsidyError.GIVE_IT_UP
    total_subsidy: Decimal = ZERO
    eligibility_status: EligibilityStatus = EligibilityStatus.GIVE_IT_UP

    def as_dict(self) -> dict[str, Any]:
        return {
            "source": SUBSIDY_SOURCE,
            "status": str(SubsidyError.GIVE_IT_UP),
            "available": False,
            "reason": self.reason,
            "engineVersion": SUBSIDY_ENGINE_VERSION,
            "subsidyType": self.subsidy_type,
            "eligibleCapacityKw": self.eligible_capacity_kw,
            "eligibilityStatus": str(EligibilityStatus.GIVE_IT_UP),
            "eligibilityReason": self.reason,
            "centralSubsidy": ZERO,
            "stateSubsidy": ZERO,
            "totalSubsidy": ZERO,
            "subsidyAmount": ZERO,
            "dcrRequired": False,
            "schemeName": self.scheme_name,
            "schemeVersion": self.scheme_version,
            "calculatedAt": self.calculated_at,
            "postSubsidyInvestment": None,
            "assumptions": {"subsidyBasis": "GIVE_IT_UP — consumer forfeited subsidy, non-DCR panels allowed", "stateTopUp": ZERO, "dcrRequirement": False},
        }


@dataclass(frozen=True)
class SubsidyNotRequested:
    """``NO_SUBSIDY``: the quotation asks for none."""

    calculated_at: str | None
    reason: str = 'Subsidy type is "none" — no subsidy requested.'
    available: bool = False
    status: SubsidyError = SubsidyError.NO_SUBSIDY
    total_subsidy: Decimal = ZERO
    eligibility_status: None = None

    def as_dict(self) -> dict[str, Any]:
        return {
            "source": SUBSIDY_SOURCE,
            "status": str(SubsidyError.NO_SUBSIDY),
            "available": False,
            "reason": self.reason,
            "engineVersion": SUBSIDY_ENGINE_VERSION,
            "subsidyType": "none",
            "eligibilityStatus": None,
            "centralSubsidy": ZERO,
            "stateSubsidy": ZERO,
            "totalSubsidy": ZERO,
            "subsidyAmount": ZERO,
            "calculatedAt": self.calculated_at,
            "postSubsidyInvestment": None,
        }


@dataclass(frozen=True)
class SubsidyBlocked:
    """``MISSING_CONFIG`` / ``INVALID_INPUT``: nothing can be stated."""

    status: SubsidyError
    reason: str
    calculated_at: str | None
    available: bool = False
    total_subsidy: None = None
    eligibility_status: None = None

    def as_dict(self) -> dict[str, Any]:
        return {
            "source": SUBSIDY_SOURCE,
            "status": str(self.status),
            "available": False,
            "reason": self.reason,
            "engineVersion": SUBSIDY_ENGINE_VERSION,
            "eligibilityStatus": None,
            "calculatedAt": self.calculated_at,
        }


SubsidyResult = SubsidyEligible | SubsidyIneligible | SubsidyGivenUp | SubsidyNotRequested | SubsidyBlocked


# ---- calculation ---------------------------------------------------------------------------------------------------


def _or(value: Decimal | None, fallback: Decimal) -> Decimal:
    return fallback if value is None else value


def _residential(size: Decimal, config: SubsidyConfig, calculated_at: str | None) -> SubsidyResult:
    rule = config.residential
    if rule is None:
        return SubsidyBlocked(SubsidyError.MISSING_CONFIG, "Subsidy config missing residential section.", calculated_at)
    name, version = _scheme(config)
    max_capacity = _or(rule.max_capacity_kw, DEFAULT_MAX_CAPACITY_KW)
    min_capacity = _or(rule.min_capacity_kw, DEFAULT_MIN_CAPACITY_KW)
    if size > max_capacity:
        reason = f"System size {js_text(size)} kW exceeds maximum eligible capacity of {js_text(max_capacity)} kW for residential subsidy."
        return SubsidyIneligible(SubsidyError.INELIGIBLE_CAPACITY, reason, SubsidyType.RESIDENTIAL, config.dcr_required, name, version, calculated_at)
    if size < min_capacity:
        reason = f"System size {js_text(size)} kW is below minimum eligible capacity of {js_text(min_capacity)} kW for residential subsidy."
        return SubsidyIneligible(SubsidyError.INELIGIBLE_CAPACITY, reason, SubsidyType.RESIDENTIAL, config.dcr_required, name, version, calculated_at)
    subsidy = ZERO
    remaining = size
    for index, tier in enumerate(rule.tiers):
        if remaining <= 0:
            break
        previous_cap = ZERO if index == 0 else rule.tiers[index - 1].up_to_kw
        units = min(remaining, tier.up_to_kw - previous_cap)
        subsidy += units * tier.rate_per_kw
        remaining -= units
    central = js_round(min(subsidy, _or(rule.max_subsidy, DEFAULT_MAX_SUBSIDY)))
    state = _or(config.state_top_up_kerala, ZERO)
    return SubsidyEligible(
        subsidy_type=SubsidyType.RESIDENTIAL,
        eligible_capacity_kw=min(size, max_capacity),
        central_subsidy=central,
        state_subsidy=state,
        total_subsidy=central + state,
        dcr_required=config.dcr_required,
        scheme_name=name,
        scheme_version=version,
        effective_period=config.scheme.effective_period if config.scheme is not None else None,
        calculated_at=calculated_at,
    )


def _ghs(size: Decimal, houses: Any, config: SubsidyConfig, calculated_at: str | None) -> SubsidyResult:
    rule = config.ghs
    if rule is None:
        return SubsidyBlocked(SubsidyError.MISSING_CONFIG, "Subsidy config missing GHS section.", calculated_at)
    if not is_number(houses) or houses <= 0 or Decimal(houses) != Decimal(houses).to_integral_value():
        return SubsidyBlocked(SubsidyError.INVALID_INPUT, "GHS houses must be a positive integer.", calculated_at)
    name, version = _scheme(config)
    per_house_kw = min(size, _or(rule.max_kw_per_house, DEFAULT_GHS_MAX_KW_PER_HOUSE))
    total_eligible_kw = min(per_house_kw * houses, _or(rule.max_community_kw, DEFAULT_GHS_MAX_COMMUNITY_KW))
    central = js_round(total_eligible_kw * _or(rule.rate_per_kw, DEFAULT_GHS_RATE_PER_KW))
    state = _or(config.state_top_up_kerala, ZERO)
    return SubsidyEligible(
        subsidy_type=SubsidyType.GHS,
        eligible_capacity_kw=per_house_kw,
        central_subsidy=central,
        state_subsidy=state,
        total_subsidy=central + state,
        dcr_required=config.dcr_required,
        scheme_name=name,
        scheme_version=version,
        effective_period=config.scheme.effective_period if config.scheme is not None else None,
        calculated_at=calculated_at,
        ghs_houses=houses,
        ghs_total_eligible_kw=total_eligible_kw,
    )


@dataclass(frozen=True)
class SubsidyInputs:
    """``calculateSubsidy``'s inputs. ``system_size_kw`` and ``ghs_houses`` count as numbers only as Decimals or ints
    (a string is ``INVALID_INPUT``, as a non-number is in the JavaScript); floats raise ``TypeError``. ``None`` is the
    JavaScript's ``null`` (``subsidy_type=None`` → NO_SUBSIDY, ``connection_type=None`` → INELIGIBLE_CONNECTION)."""

    system_size_kw: Any
    subsidy_type: str | None = SubsidyType.RESIDENTIAL
    ghs_houses: Any = 1
    panel_type: str | None = "DCR"
    connection_type: str | None = "domestic"

    def __post_init__(self) -> None:
        for name in ("system_size_kw", "ghs_houses"):
            if isinstance(getattr(self, name), float):
                raise TypeError(f"{name}: float is not accepted in an engine; pass a Decimal or an int")


@exact
def calculate_subsidy(inputs: SubsidyInputs, config: SubsidyConfig | None, *, calculated_at: str | None = None) -> SubsidyResult:
    """``calculateSubsidy`` (spec §4); ``calculated_at`` is the caller's ISO timestamp (engines never read the clock)."""
    system_size_kw, subsidy_type, ghs_houses = inputs.system_size_kw, inputs.subsidy_type, inputs.ghs_houses
    panel_type, connection_type = inputs.panel_type, inputs.connection_type
    if not subsidy_type or subsidy_type == SubsidyType.NONE:
        return SubsidyNotRequested(calculated_at)
    if config is None:
        return SubsidyBlocked(SubsidyError.MISSING_CONFIG, "No subsidy configuration provided.", calculated_at)
    if not is_number(system_size_kw) or system_size_kw <= 0:
        return SubsidyBlocked(SubsidyError.INVALID_INPUT, "System size must be a positive number.", calculated_at)
    size = Decimal(system_size_kw)
    name, version = _scheme(config)
    if connection_type != "domestic":
        reason = f'Connection type "{js_text(connection_type)}" is not eligible for PM Surya Ghar subsidy. Only domestic connections qualify.'
        return SubsidyIneligible(SubsidyError.INELIGIBLE_CONNECTION, reason, subsidy_type, config.dcr_required, name, version, calculated_at)
    is_dcr = (str(panel_type) if panel_type else "").upper() == "DCR"
    if config.dcr_required and not is_dcr and subsidy_type in (SubsidyType.RESIDENTIAL, SubsidyType.GHS):
        return SubsidyGivenUp(subsidy_type, system_size_kw, name, version, calculated_at)
    if subsidy_type == SubsidyType.RESIDENTIAL:
        return _residential(size, config, calculated_at)
    if subsidy_type == SubsidyType.GHS:
        return _ghs(size, ghs_houses, config, calculated_at)
    return SubsidyBlocked(SubsidyError.INVALID_INPUT, f'Unknown subsidy type: "{subsidy_type}". Expected "residential", "ghs", or "none".', calculated_at)
