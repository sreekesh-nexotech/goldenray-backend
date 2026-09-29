"""``pricing/cost-config/`` — the effective-dated commercial configuration (PLAN §2.3 ``pricing_cost_config``).

The keys form a closed registry (:data:`KEYS`); a PUT validates every value, closes each changed key's open row
(``effective_to`` = the new ``effective_from``) and appends the new row, in one transaction. Unchanged values write
nothing. A client that sends ``current_uid`` for a key gets 409 ``stale_version`` when someone else changed it first.

Scalars (numbers ≥ 0, whole numbers, fractions — percentages are stored as fractions, CLAUDE.md) are the website BOM
calculator's global costs (legacy ``bom.GlobalCosts`` / Flarize ``catalog.json`` ``costs``, ``transportConfig``,
``officeExpense``) and the GST split; documents are whole validated configurations the engines read as they are
(``cost_engine.config`` = Flarize ``cost-config.json``, ``rate_card`` = ``project-rate-card.json``, the four
customer-engine configurations). ``target_gross_margin_by_tier`` and the cost-engine ``margin`` section are margin
data: hidden from callers without ``pricing_internal.view``.
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from datetime import date
from decimal import Decimal, InvalidOperation

from django.db import IntegrityError, transaction

from audit.services import record
from core.errors import Conflict, DomainError, StaleVersion
from core.services import stamp_create
from flarize.cache_utils import bump
from pricing.models import CostConfig
from pricing.services.common import AUTHORING_NAMESPACE, canonical_json, json_safe, today


class ValueProblem(ValueError):
    pass


def _number(value) -> Decimal:
    if isinstance(value, bool) or value is None:
        raise ValueProblem("Must be a number.")
    try:
        number = Decimal(str(value))
    except (InvalidOperation, ValueError):
        raise ValueProblem("Must be a number.") from None
    if not number.is_finite():
        raise ValueProblem("Must be a finite number.")
    return number


def json_number(number: Decimal):
    """A Decimal as the JSON number it denotes (``int`` when whole, else ``float`` of its text)."""
    number = Decimal(number)
    if number == number.to_integral_value():
        return int(number)
    return float(format(number.normalize(), "f"))


def plain_json(value):
    """``value`` with every ``Decimal`` replaced by the JSON number it denotes (documents stay JSON-native)."""
    if isinstance(value, Decimal):
        return json_number(value)
    if isinstance(value, dict):
        return {str(key): plain_json(item) for key, item in value.items()}
    if isinstance(value, (list, tuple)):
        return [plain_json(item) for item in value]
    return value


def money_value(value):
    number = _number(value)
    if number < 0:
        raise ValueProblem("Must be ≥ 0.")
    if number != number.quantize(Decimal("0.01")):
        raise ValueProblem("At most 2 decimal places.")
    return json_number(number)


def integer_value(value):
    number = _number(value)
    if number != number.to_integral_value() or number < 0:
        raise ValueProblem("Must be a whole number ≥ 0.")
    return int(number)


def fraction_value(value):
    number = _number(value)
    if number < 0 or number > 1:
        raise ValueProblem("Must be a fraction between 0 and 1 (0.18 = 18 %).")
    return json_number(number)


def margin_by_tier_value(value):
    if not isinstance(value, dict) or not value:
        raise ValueProblem('An object {"BASE": {"target": 0.2, "minimum": 0.15}, …}.')
    result = {}
    for tier, entry in value.items():
        if tier not in ("BASE", "VALUE", "PREMIUM"):
            raise ValueProblem(f"Unknown tier {tier!r} (BASE, VALUE, PREMIUM).")
        if not isinstance(entry, dict) or "target" not in entry:
            raise ValueProblem(f"{tier}: needs a target (fraction).")
        target = fraction_value(entry["target"])
        if Decimal(str(target)) >= 1:
            raise ValueProblem(f"{tier}: a gross margin must be below 1.")
        item = {"target": target}
        if entry.get("minimum") is not None:
            minimum = fraction_value(entry["minimum"])
            if Decimal(str(minimum)) > Decimal(str(target)):
                raise ValueProblem(f"{tier}: minimum must not exceed target.")
            item["minimum"] = minimum
        result[tier] = item
    return result


def cost_engine_config_value(value):
    if not isinstance(value, dict) or not str(value.get("schema", "")).startswith("flarize.cost-config/"):
        raise ValueProblem('The Flarize cost configuration document ({"schema": "flarize.cost-config/2", …}).')
    for section in ("installation", "transportation", "officeExpenseAllocation", "miscellaneous", "gst", "margin"):
        if section in value and not isinstance(value[section], dict):
            raise ValueProblem(f"Section {section!r} must be an object.")
    return value


def rate_card_value(value):
    from engines.rate_card import CONFIG_TYPE

    if not isinstance(value, dict) or not isinstance(value.get("store"), dict) or not isinstance(value["store"].get("records", {}), dict):
        raise ValueProblem('The Project Head rate card ({"cardVersion": …, "store": {"records": {…}}}).')
    for key, versions in value["store"].get("records", {}).items():
        if not isinstance(versions, list):
            raise ValueProblem(f"Record {key!r} must be a list of versions.")
        for version in versions:
            if not isinstance(version, dict) or version.get("configType") not in CONFIG_TYPE or not version.get("versionId"):
                raise ValueProblem(f"Record {key!r} has a version without configType/versionId.")
    return value


def _engine_document(validate: Callable[[object], object]):
    def check(value):
        if not isinstance(value, dict):
            raise ValueProblem("Must be a configuration object.")
        try:
            outcome = validate(value)
        except (ValueError, TypeError) as exc:
            raise ValueProblem(str(exc)) from None
        if isinstance(outcome, tuple) and not outcome[0]:
            raise ValueProblem("; ".join(str(problem) for problem in outcome[1]) or "Invalid configuration.")
        return value

    return check


def _energy(value):
    from engines.energy import validate_energy_config

    return _engine_document(validate_energy_config)(value)


def _savings(value):
    from engines.savings import validate_savings_config

    return _engine_document(validate_savings_config)(value)


def _subsidy(value):
    from engines.subsidy import validate_subsidy_config

    return _engine_document(validate_subsidy_config)(value)


def _finance(value):
    from engines.finance import validate_finance_config

    return _engine_document(validate_finance_config)(value)


@dataclass(frozen=True)
class KeySpec:
    key: str
    group: str
    kind: str
    validate: Callable
    description: str
    internal: bool = False
    required_for_release: bool = False


def _spec(key, group, kind, validate, description, **options) -> tuple[str, KeySpec]:
    return key, KeySpec(key, group, kind, validate, description, **options)


KEYS: dict[str, KeySpec] = dict(
    [
        _spec("install_rate", "website_bom", "money", money_value, "Installation ₹ per kW (legacy GlobalCosts.install_rate)."),
        _spec("service_rate_year", "website_bom", "money", money_value, "Service ₹ per year (GlobalCosts.service_rate_year)."),
        _spec("service_years", "website_bom", "integer", integer_value, "Service years (GlobalCosts.service_years)."),
        _spec("transport_rate_per_km", "website_bom", "money", money_value, "Transport ₹ per km for the whole distance (GlobalCosts.transport_rate)."),
        _spec("transport_base_km", "website_bom", "integer", integer_value, "Distance included before the extra-km rate applies (GlobalCosts.default_dist_km)."),
        _spec("transport_extra_rate_per_km", "website_bom", "money", money_value, "₹ per km beyond transport_base_km (the legacy calculator's constant 35; catalog.json transportConfig.costPerKm)."),
        _spec("miscellaneous", "website_bom", "money", money_value, "Fixed miscellaneous ₹ per project (GlobalCosts.miscellaneous)."),
        _spec("office_per_project", "website_bom", "money", money_value, "Fixed office ₹ per project (GlobalCosts.office)."),
        _spec("office_expense_monthly", "website_bom", "money", money_value, "Office expense per month (catalog.json officeExpense.monthlyExpense)."),
        _spec("expected_projects_per_month", "website_bom", "integer", integer_value, "Projects per month the office expense is spread over."),
        _spec("elevated_structure_rate", "website_bom", "money", money_value, "Elevated structure ₹ (GlobalCosts.elevated_structure_rate)."),
        _spec("sheet_structure_rate", "website_bom", "money", money_value, "Sheet-roof structure ₹ (GlobalCosts.sheet_structure_rate)."),
        _spec("gp_rate_per_kg", "website_bom", "money", money_value, "GP tube ₹ per kg (Base tier)."),
        _spec("gi_rate_per_kg", "website_bom", "money", money_value, "GI tube ₹ per kg (Value/Premium)."),
        _spec("structure_labor", "website_bom", "money", money_value, "Structure labour ₹ per project."),
        _spec("structure_repair_pct", "website_bom", "fraction", fraction_value, "Structure repair allowance as a fraction (legacy 10 % → 0.1)."),
        _spec("gst_goods_share", "gst", "fraction", fraction_value, "Share of the price taxed as goods (0.70).", required_for_release=True),
        _spec("gst_services_share", "gst", "fraction", fraction_value, "Share taxed as services (0.30).", required_for_release=True),
        _spec("gst_goods_rate", "gst", "fraction", fraction_value, "GST rate on the goods share (0.05).", required_for_release=True),
        _spec("gst_services_rate", "gst", "fraction", fraction_value, "GST rate on the services share (0.18).", required_for_release=True),
        _spec("target_gross_margin_by_tier", "margin", "document", margin_by_tier_value, "Target/minimum gross margin per tier (fractions).", internal=True),
        _spec("cost_engine.config", "documents", "document", cost_engine_config_value, "The Flarize cost-engine configuration (cost-config.json) read by engines.cost."),
        _spec("rate_card", "documents", "document", rate_card_value, "The Project Head rate card (project-rate-card.json), versioned records read by engines.rate_card."),
        _spec("energy.config", "documents", "document", _energy, "engines.energy configuration (energy-config.json)."),
        _spec("savings.config", "documents", "document", _savings, "engines.savings configuration (savings-config.json)."),
        _spec("subsidy.config", "documents", "document", _subsidy, "engines.subsidy configuration (subsidy-config.json)."),
        _spec("finance.config", "documents", "document", _finance, "engines.finance configuration (finance-config.json)."),
    ]
)
GST_KEYS = ("gst_goods_share", "gst_services_share", "gst_goods_rate", "gst_services_rate")


def validate_value(key: str, value):
    spec = KEYS.get(key)
    if spec is None:
        raise DomainError("unknown_config_key", f"{key!r} is not a cost configuration key.", errors={key: [f"Known keys: {', '.join(sorted(KEYS))}."]})
    try:
        return spec.validate(value)
    except ValueProblem as exc:
        raise DomainError("validation_error", f"Invalid value for {key}.", errors={key: [str(exc)]}) from None


def redact(key: str, value, *, internal: bool):
    """The value a caller may see: margin data only with ``pricing_internal.view`` (``None`` = hidden)."""
    if internal:
        return value
    spec = KEYS.get(key)
    if spec is not None and spec.internal:
        return None
    if key == "cost_engine.config" and isinstance(value, dict) and "margin" in value:
        return {name: section for name, section in value.items() if name != "margin"}
    return value


def current_rows() -> dict[str, CostConfig]:
    return {row.key: row for row in CostConfig.objects.filter(effective_to__isnull=True).select_related("updated_by", "created_by").order_by("key")}


def current_values() -> dict:
    return {key: row.value for key, row in current_rows().items()}


def history_queryset(key: str | None = None):
    queryset = CostConfig.objects.select_related("created_by").order_by("key", "-effective_from", "-id")
    return queryset.filter(key=key) if key else queryset


def _same(a, b) -> bool:
    return canonical_json(a) == canonical_json(b)


def set_value(key: str, value, *, user, effective_from: date | None = None, note: str = "", source_ref: str = "", current_uid=None, created_at=None, audit: bool = True) -> CostConfig | None:
    """Append ``key = value`` (closing the open row). Returns the new row, or ``None`` when the value is unchanged."""
    value = validate_value(key, plain_json(value))
    effective_from = effective_from or today()
    previous = CostConfig.objects.select_for_update().filter(key=key, effective_to__isnull=True).first()
    if current_uid is not None and str(previous.uid if previous else "") != str(current_uid or ""):
        raise StaleVersion("stale_version", f"{key} was changed by someone else. Reload and try again.", errors={key: [f"Current row is {previous.uid if previous else 'none'}."]})
    if previous is not None and _same(previous.value, value):
        return None
    if previous is not None:
        if effective_from < previous.effective_from:
            raise DomainError(
                "effective_from_before_current", f"{key} is effective from {previous.effective_from}; a new value cannot start earlier.", errors={"effective_from": [str(previous.effective_from)]}
            )
        previous.versioned_update(user, effective_to=effective_from)
    row = CostConfig(key=key, value=value, effective_from=effective_from, note=note or "", source_ref=source_ref[:64])
    if created_at is not None:
        row.created_at = row.updated_at = created_at
    stamp_create(row, user)
    try:
        with transaction.atomic():
            row.save()
    except IntegrityError:
        raise Conflict("cost_config_conflict", f"{key} was changed at the same time; reload.") from None
    if created_at is not None:
        CostConfig.all_objects.filter(pk=row.pk).update(updated_at=created_at)
    if audit:
        record(
            "pricing.cost_config_set",
            obj=row,
            actor=user,
            before={key: json_safe(previous.value)} if previous else None,
            after={key: json_safe(value), "effective_from": str(effective_from)},
            note=note,
        )
    return row


@transaction.atomic
def put_values(*, user, entries: list[dict], effective_from: date | None = None, note: str = "") -> list[CostConfig]:
    """``PUT pricing/cost-config/``: every entry ``{key, value, current_uid?}``; returns the rows written."""
    errors = {}
    for entry in entries:
        try:
            validate_value(entry["key"], entry.get("value"))
        except DomainError as exc:
            errors.update(exc.errors)
    if errors:
        raise DomainError("validation_error", "Invalid cost configuration.", errors=errors)
    if len({entry["key"] for entry in entries}) != len(entries):
        raise DomainError("validation_error", "Each key may appear once.", errors={"entries": ["Duplicate key."]})
    written = []
    for entry in sorted(entries, key=lambda item: item["key"]):
        row = set_value(entry["key"], entry.get("value"), user=user, effective_from=effective_from, note=note, current_uid=entry.get("current_uid"))
        if row is not None:
            written.append(row)
    if written:
        bump(AUTHORING_NAMESPACE)
    return written
