"""String and MPPT layout for a panel/inverter pair (Golden-Ray-BOM ``stringConfig``).

How many panels go in series, how many such strings, and across which MPPT inputs — plus
the checks that say whether the layout is actually safe to install.

Why this matters more than it looks: a string whose cold-morning open-circuit voltage
exceeds the inverter's input rating can destroy the inverter, and a string whose
hot-afternoon operating voltage falls under the MPPT window simply stops producing. Both
are silent at design time and expensive afterwards, which is why the warnings carry
severities and a layout with any ``critical`` is reported as invalid.

Pure functions over plain dicts, like :mod:`engines.energy` and :mod:`engines.subsidy` —
no Django import, so this runs in a bare test with no database.

Two formulas look wrong and are deliberately kept:

* the Voc temperature coefficient is used for **both** Voc and Vmp. Vmp has its own
  coefficient and a correct model would use it, but every captured case depends on this
  one, so changing it would silently move 300 golden results. Flagged for a follow-up
  rather than quietly corrected.
* ``mppt_mid`` is 0.65 × the **sum** of the MPPT window bounds, not an interpolation
  between them. Almost certainly meant as a midpoint-ish heuristic; the outputs depend on
  exactly this arithmetic.
"""

from __future__ import annotations

import math
from typing import Any

#: Cell temperatures the voltage checks are run at. A panel's rating is quoted at 25 °C,
#: but a Kerala roof swings far either side: the cold-morning extreme produces the highest
#: open-circuit voltage (the danger to the inverter) and the hot-afternoon extreme the
#: lowest operating voltage (the danger to production).
T_MIN_CELL_C = 15
T_MAX_CELL_C = 70
T_STC_C = 25
#: Panel certification limit when a panel does not state its own.
DEFAULT_MAX_SYSTEM_VOLTAGE = 1500
#: Share of the MPPT window the layout aims at — see the module note on `mppt_mid`.
MPPT_TARGET_SHARE = 0.65

#: ``code -> severity``. Exported so the API, the Studio and
#: :mod:`engines.engineering_checker` share one list rather than each keeping its own.
WARNINGS: dict[str, str] = {
    "VOC_EXCEEDS_MAX": "critical",
    "VMP_BELOW_MPPT_MIN": "critical",
    "ISC_EXCEEDS_MAX": "warning",
    "STRINGS_EXCEED_MPPT": "critical",
    "VMP_ABOVE_MPPT_MAX": "warning",
    "UNEVEN_STRINGS": "info",
    "SYSTEM_VOLTAGE_EXCEEDED": "critical",
    # Upgrade-path only. `ISC_HIGH` is the upgrade counterpart of `ISC_EXCEEDS_MAX`.
    "ISC_HIGH": "warning",
    "EXIST_STRINGS_HIGH": "warning",
    "NEW_STRINGS_HIGH": "warning",
    "SINGLE_MPPT": "info",
}


def _number(value: Any) -> float | None:
    if value is None or isinstance(value, bool):
        return None
    try:
        out = float(value)
    except (TypeError, ValueError):
        return None
    return out if math.isfinite(out) else None


def string_config(*, panel: dict, inverter: dict, panels: int, is_three_phase: bool = False) -> dict | None:
    """The layout for ``panels`` of ``panel`` on ``inverter``, or ``None`` if unknowable.

    ``None`` rather than a guess: without the panel's Voc or the inverter's MPPT count
    there is nothing to compute, and a fabricated layout is worse than an absent one.
    """
    voc = _number(panel.get("voc"))
    vmp = _number(panel.get("vmp"))
    mppt_count = _number(inverter.get("mppt_count"))
    if not voc or not vmp or not mppt_count:
        return None

    # A micro-inverter sits behind each panel, so there is no string to configure.
    if inverter.get("is_micro"):
        return {
            "is_micro": True,
            "micro_count": panels,
            "panels": panels,
            "message": f"{panels} micro-inverters — one per panel, no string configuration needed",
            "warnings": [],
            "dc_cable_recommended_m": max(10, math.ceil(panels * 2)),
        }

    max_input_voltage = _number(inverter.get("max_input_voltage")) or 0
    # Optimisers condition each panel's output, so the string limit is a count, not a
    # voltage sum.
    if inverter.get("optimizer_based"):
        return {
            "is_optimizer": True,
            "panels": panels,
            "max_string_length": math.floor(max_input_voltage) if max_input_voltage else None,
            "warnings": [],
            "dc_cable_recommended_m": 50,
        }

    mppt_min = _number(inverter.get("mppt_voltage_min")) or 0
    mppt_max = _number(inverter.get("mppt_voltage_max")) or 0
    max_input_current = _number(inverter.get("max_input_current")) or 0
    max_strings_per_mppt = int(_number(inverter.get("max_strings_per_mppt")) or 1)
    isc = _number(panel.get("isc")) or 0
    temp_coeff = _number(panel.get("temp_coeff_voc")) or 0
    max_system_voltage = _number(panel.get("max_sys_voltage")) or DEFAULT_MAX_SYSTEM_VOLTAGE
    mppt_count = int(mppt_count)

    # Cold morning lifts Voc; hot roof drops Vmp. See the module note on the coefficient.
    voc_cold = voc * (1 + (temp_coeff / 100) * (T_MIN_CELL_C - T_STC_C))
    vmp_hot = vmp * (1 + (temp_coeff / 100) * (T_MAX_CELL_C - T_STC_C))

    max_per_string = math.floor(max_input_voltage / voc_cold) if voc_cold else 0
    min_per_string = math.ceil(mppt_min / vmp_hot) if vmp_hot else 0
    mppt_mid = (mppt_min + mppt_max) * MPPT_TARGET_SHARE
    optimal = round(mppt_mid / vmp) if vmp else 0
    # Clamped so the voltage limits win over the efficiency target.
    panels_per_string = max(min_per_string, min(max_per_string, optimal))
    if panels_per_string <= 0:
        return None

    total_strings = math.ceil(panels / panels_per_string)
    max_total_strings = mppt_count * max_strings_per_mppt
    strings_per_mppt = math.ceil(total_strings / mppt_count)
    slots = panels_per_string * total_strings
    empty_slots = slots - panels

    string_voc_cold = panels_per_string * voc_cold
    string_vmp_hot = panels_per_string * vmp_hot
    string_vmp_stc = panels_per_string * vmp

    warnings: list[dict] = []

    def warn(code: str, message: str) -> None:
        warnings.append({"code": code, "severity": WARNINGS[code], "message": message})

    if string_voc_cold > max_input_voltage:
        warn(
            "VOC_EXCEEDS_MAX",
            f"String Voc at {T_MIN_CELL_C}°C ({string_voc_cold:.1f}V) exceeds the inverter's maximum input "
            f"voltage ({max_input_voltage:g}V). Reduce panels per string to {math.floor(max_input_voltage / voc_cold)}.",
        )
    if string_vmp_hot < mppt_min:
        warn(
            "VMP_BELOW_MPPT_MIN",
            f"String Vmp at {T_MAX_CELL_C}°C ({string_vmp_hot:.1f}V) falls below the MPPT minimum "
            f"({mppt_min:g}V), so the inverter stops tracking. Increase panels per string to "
            f"{math.ceil(mppt_min / vmp_hot)}.",
        )
    if isc > max_input_current:
        warn(
            "ISC_EXCEEDS_MAX",
            f"Panel Isc ({isc:g}A) exceeds the inverter's maximum input current per MPPT "
            f"({max_input_current:g}A). Consider a higher-rated inverter.",
        )
    if total_strings > max_total_strings:
        warn(
            "STRINGS_EXCEED_MPPT",
            f"Needs {total_strings} strings but the inverter supports {max_total_strings} "
            f"({mppt_count} MPPTs × {max_strings_per_mppt}). Use a higher-capacity inverter or a combiner.",
        )
    if string_vmp_stc > mppt_max:
        warn(
            "VMP_ABOVE_MPPT_MAX",
            f"String Vmp at STC ({string_vmp_stc:.1f}V) exceeds the MPPT maximum ({mppt_max:g}V); "
            f"the inverter may clip power. Reduce panels per string.",
        )
    if empty_slots > 0 and total_strings > 1:
        warn(
            "UNEVEN_STRINGS",
            f"{panels} panels across {total_strings} strings of {panels_per_string} leaves "
            f"{empty_slots} empty slot{'s' if empty_slots > 1 else ''} in the last string. "
            f"Uneven strings reduce efficiency.",
        )
    if string_voc_cold > max_system_voltage:
        warn(
            "SYSTEM_VOLTAGE_EXCEEDED",
            f"String voltage ({string_voc_cold:.1f}V) exceeds the panel's maximum system voltage "
            f"({max_system_voltage:g}V), which voids its certification.",
        )

    return {
        "is_micro": False,
        "panels": panels,
        "panels_per_string": panels_per_string,
        "total_strings": total_strings,
        "strings_per_mppt": strings_per_mppt,
        "mppt_count": mppt_count,
        "max_total_strings": max_total_strings,
        "empty_slots": empty_slots,
        "min_panels_per_string": min_per_string,
        "max_panels_per_string": max_per_string,
        "string_voc_cold": round(string_voc_cold, 1),
        "string_vmp_hot": round(string_vmp_hot, 1),
        "string_vmp_stc": round(string_vmp_stc, 1),
        "mppt_allocation": _allocate(panels, panels_per_string, mppt_count, strings_per_mppt),
        "dc_cable_recommended_m": 50 if panels <= 12 else max(50, panels * 4),
        "warnings": warnings,
        # A layout with any critical finding must not be presented as installable.
        "is_valid": not any(w["severity"] == "critical" for w in warnings),
    }


def _allocate(panels: int, per_string: int, mppt_count: int, strings_per_mppt: int) -> list[dict]:
    """Fill each MPPT in turn — greedy, as the source does, not balanced."""
    out = []
    remaining = panels
    for index in range(mppt_count):
        if remaining <= 0:
            break
        strings = min(strings_per_mppt, math.ceil(remaining / per_string))
        allocated = min(strings * per_string, remaining)
        out.append({"mppt": index + 1, "strings": strings, "panels": allocated, "label": None})
        remaining -= allocated
    return out


def upgrade_string_config(*, panel: dict, inverter: dict, from_kw: float, new_panels: int) -> dict | None:
    """Layout for an upgrade, keeping the existing array and the new one apart.

    MPPT 1 carries what is already installed and MPPT 2 the extension, rather than
    spreading panels evenly: mixing arrays of different age and orientation on one MPPT
    drags the whole string down to the weaker one's output. With a single-MPPT inverter
    that separation is impossible, which is what ``SINGLE_MPPT`` reports.

    Note this path raises no voltage criticals, so ``is_valid`` is always true — the same
    as the source. The existing array's voltages were validated when it was installed.
    """
    voc = _number(panel.get("voc"))
    vmp = _number(panel.get("vmp"))
    mppt_count = int(_number(inverter.get("mppt_count")) or 0)
    if not voc or not vmp or not mppt_count:
        return None
    if inverter.get("is_micro") or inverter.get("optimizer_based"):
        return None

    temp_coeff = _number(panel.get("temp_coeff_voc")) or 0
    mppt_min = _number(inverter.get("mppt_voltage_min")) or 0
    mppt_max = _number(inverter.get("mppt_voltage_max")) or 0
    max_input_voltage = _number(inverter.get("max_input_voltage")) or 0
    max_input_current = _number(inverter.get("max_input_current")) or 0
    max_strings_per_mppt = int(_number(inverter.get("max_strings_per_mppt")) or 1)
    isc = _number(panel.get("isc")) or 0
    watt = _number(panel.get("watt")) or 540

    voc_cold = voc * (1 + (temp_coeff / 100) * (T_MIN_CELL_C - T_STC_C))
    vmp_hot = vmp * (1 + (temp_coeff / 100) * (T_MAX_CELL_C - T_STC_C))
    max_pps = math.floor(max_input_voltage / voc_cold) if voc_cold else 0
    min_pps = math.ceil(mppt_min / vmp_hot) if vmp_hot else 0
    per_string = max(min_pps, min(max_pps, round((mppt_min + mppt_max) * MPPT_TARGET_SHARE / vmp)))
    if per_string <= 0:
        return None

    # The existing array's panel count is inferred from its kW — the old install is not
    # itemised anywhere, only its size is known.
    existing = math.ceil((from_kw * 1000) / watt)
    warnings: list[dict] = []

    def warn(code: str, message: str) -> None:
        warnings.append({"code": code, "severity": WARNINGS[code], "message": message})

    allocation = []
    if mppt_count >= 2:
        existing_strings = math.ceil(existing / per_string) if existing else 0
        new_strings = math.ceil(new_panels / per_string) if new_panels else 0
        if existing:
            allocation.append({"mppt": 1, "strings": existing_strings, "panels": existing, "label": "Existing array"})
            allocation.append({"mppt": 2, "strings": new_strings, "panels": new_panels, "label": "New panels"})
        else:
            allocation.append({"mppt": 1, "strings": new_strings, "panels": new_panels, "label": "New panels"})
        if existing_strings > max_strings_per_mppt:
            warn("EXIST_STRINGS_HIGH", f"The existing array needs {existing_strings} strings on one MPPT, which takes more than {max_strings_per_mppt}. A combiner box is required.")
        if new_strings > max_strings_per_mppt:
            warn("NEW_STRINGS_HIGH", f"The new panels need {new_strings} strings on one MPPT, which takes more than {max_strings_per_mppt}. A combiner box is required.")
    else:
        total = existing + new_panels
        allocation.append({"mppt": 1, "strings": math.ceil(total / per_string), "panels": total, "label": "Combined"})
        warn("SINGLE_MPPT", "This inverter has one MPPT, so the existing and new panels share it. Mismatched arrays on one tracker pull output down to the weaker one.")
    if isc > max_input_current:
        # `ISC_HIGH`, not the standard path's `ISC_EXCEEDS_MAX`: on an upgrade the
        # existing array's current was already accepted when it was installed, so this is
        # a note about the combined system rather than a fresh design fault
        # (App.jsx:2350).
        warn("ISC_HIGH", f"Panel Isc ({isc:g}A) exceeds the inverter's maximum input current per MPPT ({max_input_current:g}A).")

    return {
        "panels_per_string": per_string,
        "existing_panels": existing,
        "new_panels": new_panels,
        "mppt_allocation": allocation,
        "dc_cable_recommended_m": 50,
        "warnings": warnings,
        "is_valid": not any(w["severity"] == "critical" for w in warnings),
    }
