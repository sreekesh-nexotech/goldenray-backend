"""The three website calculators of the legacy main backend, as pure functions (PLAN §3.3 ``calculators/*``).

=====================  ==================================================================  =====================
Function               Legacy endpoint (``goldenray/views/…``)                             Platform endpoint
=====================  ==================================================================  =====================
:func:`basic`          ``POST /api/calculate-solar/`` (``solar_calculator_views``)         ``calculators/basic/``
:func:`basic_v2`       ``POST /api/calculate-solar-new/`` (``solar_calculator_new_views``) ``calculators/basic-v2/``
:func:`advanced`       ``POST /api/calculate-solar-advanced/`` (``solar_advanced_calc_…``) ``calculators/advanced/``
=====================  ==================================================================  =====================

Each function takes the request body exactly as parsed from JSON (any JSON value) plus the lookup tables the legacy
view queried, and returns the legacy response body **key for key and value for value**, or raises
:class:`CalculatorError` with the legacy status and message.

Faithful on purpose (docs/decisions/calculators-emi.md, DV-81):

* **Binary64 arithmetic in the legacy operation order.** The legacy views computed in Python floats (``float()`` of
  the Decimal columns, ``round(x, 2)``, ``round(x)``, ``-(-x // 4)``, ``(1 + r) ** i``); the published figures
  (units, kW, EMI paise, the 25-year graph) depend on that arithmetic, so it is kept operation for operation.
  ``engines.energy`` (the Flarize bill → units search with KSEB fixed charges, duty and meter rent),
  ``engines.subsidy`` (PM Surya Ghar per-kW tiers) and ``engines.finance`` (EMI rounded to whole rupees) are
  different formulas and are not used.
* **Lookups compare like the ORM did** (:mod:`engines.legacy_lookups`): ``str()`` of a non-text value, ``iexact`` as
  PostgreSQL ``UPPER()`` (one character for one), integer truncation/ceil of float bounds, ``DecimalField`` rounding
  of floats.
* **A legacy crash is a 400.** Whatever raised an unhandled exception in the legacy view (a non-object body, a text
  where a number is needed, ``inf``, a NUL character, a missing tariff table …) raises
  ``CalculatorError("invalid_input", …, 400)`` here (PLAN §6 approved difference: legacy 500 → 400).
"""

from __future__ import annotations

import decimal
from collections.abc import Callable, Sequence
from dataclasses import dataclass, field
from decimal import Decimal

from engines.legacy_lookups import LegacyCrash, check_renderable, crash_detail, decimal_param, float_exact, iexact_param, iexact_text, int_gte, int_lte, pg_upper, text_param

INVALID_INPUT_MESSAGE = "The calculator cannot process these inputs."
LIGHT_WATTS = 15.0  # the advanced calculator's built-in "Light" device
LIGHT_K_VALUE = 0.1
DEFAULT_BACKUP_HOURS = 3  # getattr(SolarInstallationNew, "default_backup_hours", 3): the column never existed
BATTERY_CAPACITY_MAX_DIGITS = 10  # legacy ``batteries.battery_capacity`` numeric(10,2): float lookups rounded to 10 digits
GRAPH_YEARS = (0, 5, 10, 15, 20, 25)
GRAPH_ESCALATION = 0.05
WITH_SOLAR_BILL_PER_CYCLE = 280
YEARS_TO_BREAKEVEN = 10
EMI_TENURE_YEARS = 10
BASIC_FIXED_CHARGES = 190 + 6 + (6 * 0.18)
# calculate-solar-new bill bands: (first bill, last bill) → the ``bill_range`` looked up (the upper end)
BILL_BANDS = ((6001, 8000), (8001, 10000), (10001, 15500), (15501, 20000), (20001, 24000), (24001, 30000), (30001, 40000))
FIRST_BILL_RANGE = 6000


class CalculatorError(Exception):
    """A response the legacy endpoint gave as an error (``{"error": message}``), with its HTTP status."""

    def __init__(self, code: str, message: str, status: int = 400, detail: str = ""):
        super().__init__(message)
        self.code = code
        self.message = message
        self.status = status
        self.detail = detail


# ── lookup tables (what the legacy views queried) ─────────────────────────────────────────────────────────────────
@dataclass(frozen=True)
class TariffSlab:
    """A KSEB slab (legacy ``kseb_tariffs``); ``position`` is the legacy primary-key order (``.first()``)."""

    min_units: int
    max_units: int | None
    rate: Decimal
    position: int = 0


@dataclass(frozen=True)
class DeviceType:
    name: str
    watts: int | None
    k_value: float | None
    position: int = 0


@dataclass(frozen=True)
class Vehicle:
    model: str
    energy_consumption: float | None
    k_value: float | None
    position: int = 0


@dataclass(frozen=True)
class CapacitySize:
    """Legacy ``solar_installations`` (one row per kW): what ``calculate-solar`` priced."""

    power_capacity: Decimal
    time_to_complete: int
    total_cost: Decimal
    total_subsidy: Decimal
    area_required: int


@dataclass(frozen=True)
class BillRangeSize:
    """Legacy ``solar_installation_new`` (one row per bill range and property type)."""

    bill_range: int
    property_type: str  # the stored label ("Residential", "Commercial")
    power_capacity: Decimal
    time_to_complete: str
    total_cost: Decimal
    total_subsidy: Decimal
    area_required: int
    loan_available: str
    per_kw_rate: Decimal | None = None
    final_cost: Decimal | None = None
    interest_rate: Decimal | None = None  # percent (6.50), as the legacy column
    inverter_price: Decimal | None = None
    position: int = 0


@dataclass(frozen=True)
class Battery:
    capacity: Decimal  # kWh
    price: Decimal
    position: int = 0


@dataclass(frozen=True)
class CalculatorData:
    tariffs: tuple[TariffSlab, ...] = ()
    capacity_sizes: tuple[CapacitySize, ...] = ()
    bill_range_sizes: tuple[BillRangeSize, ...] = ()
    device_types: tuple[DeviceType, ...] = ()
    ev_cars: tuple[Vehicle, ...] = ()
    ev_scooters: tuple[Vehicle, ...] = ()
    batteries: tuple[Battery, ...] = ()
    pincodes: frozenset[str] = field(default_factory=frozenset)


def _run(compute: Callable[[], dict]) -> dict:
    """Run a port inside the legacy's default decimal context; unhandled exceptions become ``invalid_input``."""
    try:
        with decimal.localcontext(decimal.Context()):
            result = compute()
            check_renderable(result)
            return result
    except CalculatorError:
        raise
    except (LegacyCrash, ArithmeticError, AttributeError, LookupError, TypeError, ValueError) as exc:
        raise CalculatorError("invalid_input", INVALID_INPUT_MESSAGE, 400, detail=crash_detail(exc)) from exc


def _first_by(rows, key: Callable) -> dict:
    """``{key(row): row}`` keeping, for each key, the first row in the legacy primary-key order (``position``)."""
    index: dict = {}
    for row in sorted(rows, key=lambda row: row.position):
        name = key(row)
        if name is not None:
            index.setdefault(name, row)
    return index


def _pincode_exists(data: CalculatorData, value) -> bool:
    """``Pincode.objects.filter(pincode=value).exists()``."""
    param = text_param(value)
    return param is not None and param in data.pincodes


def _body_get(body, key, default=None):
    """``request.data.get(key)`` — a JSON body that is not an object has no ``get`` (legacy: AttributeError, 500)."""
    return body.get(key, default)


def _loan_amount(loan_available) -> int:
    """``"2,00,000-6,00,000"`` → 200000 (the lower end); unparseable → 0."""
    loan_str = str(loan_available).replace(",", "")
    if "-" in loan_str:
        try:
            return int(loan_str.split("-")[0])
        except ValueError:
            return 0
    try:
        return int(loan_str)
    except ValueError:
        return 0


def _without_solar(bill, cycles_per_year, years, rate=GRAPH_ESCALATION) -> list:
    annual_bill = bill * cycles_per_year
    cumulative = []
    for y in years:
        year_bill = sum([annual_bill * ((1 + rate) ** i) for i in range(y)])
        cumulative.append(round(year_bill))
    return cumulative


def _with_solar(initial_cost, loan_amount, years_to_breakeven, years, cycles_per_year, subsidy, rate=GRAPH_ESCALATION) -> list:
    bill_per_cycle = WITH_SOLAR_BILL_PER_CYCLE
    annual_bill = bill_per_cycle * cycles_per_year
    loan_repayment_per_year = loan_amount / years_to_breakeven if years_to_breakeven else 0
    cumulative = []
    for y in years:
        if y == 0:
            cumulative.append(initial_cost - subsidy)
        elif y <= years_to_breakeven:
            year_bill = sum([annual_bill * ((1 + rate) ** i) for i in range(y)])
            total = (initial_cost - subsidy) + year_bill + loan_repayment_per_year * y
            cumulative.append(round(total))
        else:
            year_bill = sum([annual_bill * ((1 + rate) ** i) for i in range(y)])
            total = (initial_cost - subsidy) + year_bill + loan_amount
            cumulative.append(round(total))
    return cumulative


def emi_with_interest(principal, interest_rate, tenure_years=EMI_TENURE_YEARS) -> dict:
    """``goldenray/utils/finance.py`` ``emi_calc`` (used by ``calculate-solar-new`` and the EMI calculator)."""
    if principal <= 0 or interest_rate <= 0 or tenure_years <= 0:
        return {"emi_per_month": 0, "total_payment": 0, "total_interest": 0}
    monthly_interest_rate = interest_rate / (12 * 100)
    tenure_months = tenure_years * 12
    growth = (1 + monthly_interest_rate) ** tenure_months
    emi_per_month = (principal * monthly_interest_rate * growth) / (growth - 1)
    total_payment = emi_per_month * tenure_months
    total_interest = total_payment - principal
    return {"emi_per_month": round(emi_per_month, 2), "total_payment": round(total_payment, 2), "total_interest": round(total_interest, 2)}


def emi_advanced(final_cost, interest_rate, tenure_years=EMI_TENURE_YEARS) -> dict:
    """The advanced calculator's own ``emi_calc`` (no ``total_interest``; zeros when either input is not positive)."""
    if final_cost <= 0 or interest_rate <= 0:
        return {"emi_per_month": 0, "total_payment": 0}
    monthly_interest_rate = interest_rate / (12 * 100)
    tenure_months = tenure_years * 12
    emi_numerator = final_cost * monthly_interest_rate * ((1 + monthly_interest_rate) ** tenure_months)
    emi_denominator = ((1 + monthly_interest_rate) ** tenure_months) - 1
    emi_per_month = emi_numerator / emi_denominator
    total_payment = emi_per_month * tenure_months
    return {"emi_per_month": round(emi_per_month, 2), "total_payment": round(total_payment, 2)}


# ── calculate-solar ────────────────────────────────────────────────────────────────────────────────────────────────
def basic(body, data: CalculatorData) -> dict:
    """``POST calculate-solar/``: bill → units through the slabs, kW from 1.3 × daily units / 4, price by kW."""
    return _run(lambda: _basic(body, data))


def _basic(body, data: CalculatorData) -> dict:
    monthly_bill = _body_get(body, "monthly_bill")
    pincode = _body_get(body, "pincode")
    property_type = _body_get(body, "property_type")
    if not all([monthly_bill, pincode, property_type]):
        raise CalculatorError("missing_fields", "Missing required fields")
    if not isinstance(monthly_bill, (int, float)) or monthly_bill < 0:
        raise CalculatorError("invalid_monthly_bill", "Invalid monthly bill")
    if not _pincode_exists(data, pincode):
        raise CalculatorError("pincode_not_found", "Pincode not found in database", 404)

    slabs = sorted(data.tariffs, key=lambda slab: (slab.min_units, slab.position))
    tariff_slabs = [(slab.max_units if slab.max_units is not None else float("inf"), float(slab.rate)) for slab in slabs]
    fixed_charges = BASIC_FIXED_CHARGES
    base_amount = monthly_bill - fixed_charges
    remaining_amount = base_amount
    units = 0
    previous_limit = 0
    for limit, rate in tariff_slabs:
        slab_width = limit - previous_limit
        max_slab_amount = slab_width * rate
        if remaining_amount <= max_slab_amount:
            units += remaining_amount / rate
            break
        units += slab_width
        remaining_amount -= max_slab_amount
        previous_limit = limit
    daily_units = units / 30
    buffered_daily_units = daily_units * 1.3
    solar_kw = -(-buffered_daily_units // 4)
    result = {
        "estimated_units": round(units, 2),
        "daily_consumption": round(daily_units, 2),
        "buffered_daily": round(buffered_daily_units, 2),
        "solar_capacity_kW": int(solar_kw),
    }
    matches = float_exact(result["solar_capacity_kW"])  # SolarInstallation.objects.get(power_capacity=kW)
    installation = next((row for row in data.capacity_sizes if matches(float(row.power_capacity))), None)
    if installation is not None:
        result.update(
            {
                "area_required": installation.area_required,
                "installation_time_days": installation.time_to_complete,
                "total_cost": float(installation.total_cost),
                "subsidy": float(installation.total_subsidy),
            }
        )
    else:
        result.update({"area_required": None, "installation_time_days": None, "total_cost": None, "subsidy": None})
    result["pincode"] = pincode
    result["property_type"] = property_type
    return result


# ── calculate-solar-new ────────────────────────────────────────────────────────────────────────────────────────────
def bill_range_for(monthly_bill: int) -> int | None:
    """The legacy bill bands: ≤ 6000 → 6000, 6001–8000 → 8000, … 30001–40000 → 40000; above → ``None``."""
    if monthly_bill <= FIRST_BILL_RANGE:
        return FIRST_BILL_RANGE
    for first, last in BILL_BANDS:
        if first <= monthly_bill <= last:
            return last
    return None


def basic_v2(body, data: CalculatorData) -> dict:
    """``POST calculate-solar-new/``: the bill band's row (by property type), a 25-year graph and a 10-year EMI."""
    return _run(lambda: _basic_v2(body, data))


def _basic_v2(body, data: CalculatorData) -> dict:
    monthly_bill = _body_get(body, "monthly_bill")
    pincode = _body_get(body, "pincode")
    property_type = _body_get(body, "property_type")
    if not all([monthly_bill, pincode, property_type]):
        raise CalculatorError("missing_fields", "Missing required fields")
    try:
        monthly_bill = int(monthly_bill)
    except (ValueError, TypeError):
        raise CalculatorError("invalid_monthly_bill", "Invalid monthly_bill value") from None
    if monthly_bill < 0:
        raise CalculatorError("invalid_monthly_bill", "Invalid monthly_bill value")
    if not _pincode_exists(data, pincode):
        raise CalculatorError("pincode_not_found", "Pincode not found in database", 404)
    bill_range = bill_range_for(monthly_bill)
    if bill_range is None:
        raise CalculatorError("bill_out_of_range", "Monthly bill out of supported range")
    type_matches = iexact_text(property_type)
    rows = [row for row in data.bill_range_sizes if row.bill_range == bill_range and type_matches(row.property_type)]
    if not rows:
        raise CalculatorError("no_sizing_row", "No data found for the given bill range and property type", 404)
    row = rows[0]

    loan_amount = _loan_amount(row.loan_available)
    initial_cost = float(row.total_cost)
    subsidy = float(row.total_subsidy)
    years_to_breakeven = YEARS_TO_BREAKEVEN
    # KSEB rate for 50 units: fetched (and required to exist) by the legacy view, never used in the figures.
    tariff = next((slab for slab in sorted(data.tariffs, key=lambda slab: slab.position) if slab.min_units <= 50 and slab.max_units is not None and slab.max_units >= 50), None)
    if not tariff:
        tariff = min(data.tariffs, key=lambda slab: (slab.min_units, slab.position), default=None)
    if tariff is None:
        raise LegacyCrash("'NoneType' object has no attribute 'rate'")
    bill_cycles_per_year = 12 if property_type.lower() == "commercial" else 6
    without_solar = _without_solar(monthly_bill, bill_cycles_per_year, list(GRAPH_YEARS))
    with_solar = _with_solar(initial_cost, loan_amount, years_to_breakeven, list(GRAPH_YEARS), bill_cycles_per_year, subsidy)
    savings = without_solar[-1] - with_solar[-1]
    emi_details = emi_with_interest(principal=float(row.final_cost), interest_rate=float(row.interest_rate) if row.interest_rate else 0, tenure_years=EMI_TENURE_YEARS)
    return {
        "solar_capacity_kW": float(row.power_capacity),
        "area_required": row.area_required,
        "installation_time_days": row.time_to_complete,
        "total_cost": float(row.total_cost),
        "subsidy": float(row.total_subsidy),
        "final_cost": float(row.final_cost),
        "interest_rate": float(row.interest_rate) if row.interest_rate else 0,
        "loan_available": row.loan_available,
        "emi_details": emi_details,
        "pincode": pincode,
        "property_type": property_type,
        "datasets": [{"data": without_solar}, {"data": with_solar}],
        "savings": savings,
    }


# ── calculate-solar-advanced ───────────────────────────────────────────────────────────────────────────────────────
def advanced(body, data: CalculatorData) -> dict:
    """``POST calculate-solar-advanced/``: base load + devices + EVs → bill → residential row; hybrid adds a battery."""
    return _run(lambda: _Advanced(data).run(body))


class _Advanced:
    """``SolarAdvancedCalcAPIView.post``, line by line; each ORM query becomes a lookup over the tables."""

    def __init__(self, data: CalculatorData):
        self.data = data
        # Name indexes built once per calculation, each keeping the first row in the legacy primary-key order (the
        # ORM's `.first()`): a request may list tens of thousands of devices, and matching each one against every
        # row (UPPER() of every name, per device) cost ~1 s of CPU for a 2 MB anonymous body.
        self.devices_by_upper_name = _first_by(data.device_types, lambda device: None if device.name is None else pg_upper(device.name))
        self.cars_by_model = _first_by(data.ev_cars, lambda vehicle: vehicle.model)
        self.scooters_by_model = _first_by(data.ev_scooters, lambda vehicle: vehicle.model)

    def slab_at_most(self, units) -> TariffSlab | None:
        """``KSEBTariff.objects.filter(min_units__lte=units).order_by("-min_units").first()``."""
        matches = int_lte(units)
        return min((slab for slab in self.data.tariffs if matches(slab.min_units)), key=lambda slab: (-slab.min_units, slab.position), default=None)

    def estimate_units_from_bill(self, bill_amount):
        for slab in sorted(self.data.tariffs, key=lambda slab: (-slab.min_units, slab.position)):
            rate = float(slab.rate)
            min_units = int(slab.min_units)
            max_units = int(slab.max_units) if slab.max_units is not None else None
            estimated_units = bill_amount / rate
            if max_units is not None:
                if min_units <= estimated_units <= max_units:
                    return estimated_units, rate
            elif estimated_units >= min_units:
                return estimated_units, rate
        first_slab = min(self.data.tariffs, key=lambda slab: (slab.min_units, slab.position), default=None)
        return bill_amount / float(first_slab.rate), float(first_slab.rate)

    def device_type(self, name) -> DeviceType | None:
        """``DeviceType.objects.filter(name__iexact=name).first()``."""
        param = iexact_param(name)
        return None if param is None else self.devices_by_upper_name.get(param)

    def vehicle(self, model) -> Vehicle | None:
        """``EVCar.objects.filter(model=model).first() or EVScooter.objects.filter(model=model).first()``."""
        param = text_param(model)
        if param is None:
            return None
        return self.cars_by_model.get(param) or self.scooters_by_model.get(param)

    def device_watts(self, device_type_name):
        dt = self.device_type(device_type_name)
        wattage = float(dt.watts) if dt and dt.watts else 0
        k_value = float(dt.k_value) if dt and dt.k_value else 1.0
        return wattage, k_value

    def base_units(self, home_type, specs):
        """Units of the existing load: the new home's estimate, or the average bill through the slabs."""
        base_units = 0
        if home_type == "New Home":
            estimated_base_load = specs.get("estimated_base_load")
            if estimated_base_load:
                try:
                    base_units = float(estimated_base_load)
                except (ValueError, TypeError):
                    base_units = 0
        else:
            average_bill = specs.get("average_bill")
            if average_bill:
                try:
                    average_bill = float(average_bill)
                    base_units, _ = self.estimate_units_from_bill(average_bill)
                except (ValueError, TypeError):
                    base_units = 0
        return base_units

    def residential_row(self, bill) -> BillRangeSize | None:
        """The smallest residential bill range ≥ ``bill``, else the largest residential row."""
        at_least = int_gte(bill)
        residential = iexact_text("Residential")
        rows = [row for row in self.data.bill_range_sizes if residential(row.property_type)]
        row = min((row for row in rows if at_least(row.bill_range)), key=lambda row: (row.bill_range, row.position), default=None)
        if row is None:
            row = min(rows, key=lambda row: (-row.bill_range, row.position), default=None)
        return row

    def battery_at_least(self, capacity_kwh) -> Battery | None:
        """``Battery.objects.filter(battery_capacity__gte=kWh).order_by("battery_capacity").first()``."""
        threshold = decimal_param(capacity_kwh, BATTERY_CAPACITY_MAX_DIGITS)
        return min((battery for battery in self.data.batteries if battery.capacity >= threshold), key=lambda battery: (battery.capacity, battery.position), default=None)

    def largest_battery(self) -> Battery | None:
        return min(self.data.batteries, key=lambda battery: (-battery.capacity, battery.position), default=None)

    def run(self, data) -> dict:
        specs = data.get("Specifications", {})
        usage = data.get("usageDetails", {})
        preference = data.get("preferenceDetails", {})
        home_type = specs.get("home_type")
        grid_type = specs.get("grid_type")
        if grid_type not in ["On Grid", "Hybrid"]:
            raise CalculatorError("unsupported_grid_type", "Only On Grid and Hybrid supported in this version.")
        bill_frequency = specs.get("bill_frequency") or "BI-Monthly"
        days = 30 if bill_frequency == "Monthly" else 60

        total_device_kwh_per_day = 0
        for device in usage.get("usage_electronic_devices", []):
            device_type_name = device.get("device_type")
            if device_type_name and device_type_name.lower() == "light":
                wattage, k_value = LIGHT_WATTS, LIGHT_K_VALUE
            else:
                wattage, k_value = self.device_watts(device_type_name)
            daily_usage = float(device.get("daily_usage", 0))
            no_of_units = int(device.get("no_of_units", 1))
            total_device_kwh_per_day += (wattage * daily_usage * no_of_units * k_value) / 1000
        total_device_units = total_device_kwh_per_day * days

        total_ev_kwh_per_day = 0
        for ev in usage.get("electric_vehicles", []):
            model = ev.get("model")
            daily_avg_km = float(ev.get("daily_avg_km", 0))
            no_of_vehicles = int(ev.get("no_of_vehicles", 1))
            ev_obj = self.vehicle(model)
            if ev_obj and ev_obj.energy_consumption:
                energy_consumption = float(ev_obj.energy_consumption)
                k_value = float(ev_obj.k_value) if ev_obj.k_value else 1.0
                total_ev_kwh_per_day += daily_avg_km * energy_consumption * no_of_vehicles * k_value
        total_ev_units = total_ev_kwh_per_day * days

        base_units = self.base_units(home_type, specs)
        total_units = base_units + total_device_units + total_ev_units
        slab = self.slab_at_most(total_units)
        kseb_rate = float(slab.rate) if slab else 6.75
        new_bimonthly_bill = total_units * kseb_rate

        solar_row = self.residential_row(new_bimonthly_bill)
        if solar_row is None:
            raise CalculatorError("no_matching_installation", "No matching solar installation found for the calculated bill and type Residential.", 404)
        emi_details = emi_advanced(
            final_cost=float(solar_row.final_cost),
            interest_rate=float(solar_row.interest_rate) if solar_row.interest_rate else 0,
            tenure_years=EMI_TENURE_YEARS,
        )
        response_data = {
            "bill_range": solar_row.bill_range,
            "power_capacity": float(solar_row.power_capacity),
            "time_to_complete": solar_row.time_to_complete,
            "overall_setup_cost": float(solar_row.total_cost),
            "total_subsidy": float(solar_row.total_subsidy),
            "emi_details": emi_details,
            "area_required": solar_row.area_required,
            "loan_available": solar_row.loan_available,
            "per_kw_rate": float(solar_row.per_kw_rate) if solar_row.per_kw_rate is not None else None,
            "final_cost": float(solar_row.final_cost) if solar_row.final_cost is not None else None,
            "interest_rate": float(solar_row.interest_rate) if solar_row.interest_rate is not None else None,
            "type": solar_row.property_type,
        }
        if grid_type == "Hybrid":
            self.hybrid(preference, solar_row, response_data)

        # The virtual bill of the planned devices and EVs.
        virtual_monthly_kwh = (total_device_kwh_per_day + total_ev_kwh_per_day) * 30
        tariff_row_virtual = self.slab_at_most(virtual_monthly_kwh)
        kseb_rate_virtual = 6.67
        if tariff_row_virtual and (tariff_row_virtual.max_units is None or virtual_monthly_kwh <= tariff_row_virtual.max_units):
            kseb_rate_virtual = float(tariff_row_virtual.rate)
        virtual_monthly_bill = virtual_monthly_kwh * kseb_rate_virtual

        # The legacy view recomputed the base units here with the same code (same inputs, same result).
        slab = self.slab_at_most(base_units)
        kseb_rate = float(slab.rate) if slab else 6.75
        base_bimonthly_bill = base_units * kseb_rate
        total_bimonthly_bill_for_graph = base_bimonthly_bill + (virtual_monthly_bill * 2 if virtual_monthly_bill else 0)

        initial_cost = float(solar_row.total_cost)
        if grid_type == "Hybrid" and "overall_setup_cost" in response_data:
            initial_cost = float(response_data["overall_setup_cost"])
        subsidy = float(solar_row.total_subsidy)
        loan_amount = _loan_amount(solar_row.loan_available)
        without_solar = _without_solar(total_bimonthly_bill_for_graph, 6, list(GRAPH_YEARS))
        with_solar = _with_solar(initial_cost, loan_amount, YEARS_TO_BREAKEVEN, list(GRAPH_YEARS), 6, subsidy)
        response_data["graph_without_solar"] = without_solar
        response_data["graph_with_solar"] = with_solar
        if without_solar and with_solar:
            response_data["savings"] = without_solar[-1] - with_solar[-1]
        return response_data

    def hybrid(self, preference, solar_row: BillRangeSize, response_data: dict) -> None:
        """Backup battery: the smallest battery holding the backup devices' energy, else the largest one."""
        backup_hours = preference.get("backup_hours")
        preference_devices = preference.get("preference_electronic_devices", [])
        if (not backup_hours or float(backup_hours) == 0) and not preference_devices:
            backup_hours = DEFAULT_BACKUP_HOURS
            all_devices = [
                {"device_type": "Light", "no_of_units": 3, "daily_usage": backup_hours},
                {"device_type": "Fan", "no_of_units": 2, "daily_usage": backup_hours},
            ]
            response_data["default_backup_hours"] = backup_hours
        else:
            all_devices = preference_devices + [
                {"device_type": "Light", "no_of_units": 3, "daily_usage": float(backup_hours) if backup_hours else 0},
                {"device_type": "Fan", "no_of_units": 2, "daily_usage": float(backup_hours) if backup_hours else 0},
            ]

        total_required_battery_capacity = 0
        total_backup_watts = 0
        for device in all_devices:
            device_type_name = device.get("device_type")
            if device_type_name.lower() == "light":
                wattage, k_value = LIGHT_WATTS, LIGHT_K_VALUE
            else:
                wattage, k_value = self.device_watts(device_type_name)
            no_of_units = int(device.get("no_of_units", 1))
            daily_usage = float(device.get("daily_usage", 0))
            total_required_battery_capacity += (wattage * no_of_units * daily_usage * k_value) / 1000
            total_backup_watts += wattage * no_of_units

        average_load_kw = total_required_battery_capacity / float(backup_hours) if backup_hours and float(backup_hours) > 0 else 0
        selected_battery = self.battery_at_least(total_required_battery_capacity)
        if selected_battery:
            actual_backup_time = float(selected_battery.capacity) / average_load_kw if average_load_kw > 0 else 0
            response_data.update(_battery_fields(selected_battery, solar_row, response_data, total_required_battery_capacity, total_backup_watts, average_load_kw, actual_backup_time))
            return
        largest_battery = self.largest_battery()
        if largest_battery and average_load_kw > 0:
            actual_backup_time = float(largest_battery.capacity) / average_load_kw
            fields = _battery_fields(largest_battery, solar_row, response_data, total_required_battery_capacity, total_backup_watts, average_load_kw, actual_backup_time)
            fields["battery_info"] = (
                f"No battery can provide {backup_hours} hours of backup for your specified devices (need {total_required_battery_capacity:.2f} kWh). "
                f"The largest available battery can provide up to {round(float(actual_backup_time), 2)} hours of backup."
            )
            response_data.update(fields)
        else:
            response_data["battery_info"] = (
                f"No battery found that can provide {backup_hours} hours of backup for your specified devices, which require {total_required_battery_capacity:.2f} kWh of energy."
            )


def _battery_fields(battery: Battery, solar_row: BillRangeSize, response_data: dict, required, total_backup_watts, average_load_kw, actual_backup_time) -> dict:
    inverter_price = float(solar_row.inverter_price) if solar_row.inverter_price is not None else 0
    total_battery_cost = float(battery.price) + inverter_price
    overall_setup_cost = float(response_data.get("final_cost", 0)) + total_battery_cost + float(response_data.get("total_cost", 0))
    final_cost = overall_setup_cost + float(response_data.get("total_subsidy", 0))
    return {
        "battery_capacity": float(battery.capacity),
        "battery_price": float(battery.price),
        "inverter_price": inverter_price,
        "total_battery_cost": total_battery_cost,
        "calculated_required_capacity": round(float(required), 2),
        "total_backup_watts": total_backup_watts,
        "average_load_kw": round(float(average_load_kw), 2),
        "actual_backup_time": round(float(actual_backup_time), 2),
        "overall_setup_cost": final_cost,
        "final_cost": overall_setup_cost,
    }


__all__: Sequence[str] = (
    "Battery",
    "BillRangeSize",
    "CalculatorData",
    "CalculatorError",
    "CapacitySize",
    "DeviceType",
    "TariffSlab",
    "Vehicle",
    "advanced",
    "basic",
    "basic_v2",
    "bill_range_for",
    "emi_advanced",
    "emi_with_interest",
)
