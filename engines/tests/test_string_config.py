"""``engines.string_config`` — string and MPPT layout, replayed against the BOM corpus.

No database: the engine is pure, so these run in milliseconds and can be used as a tight
loop while working on it.

The corpus under ``bom/tests/parity/source`` was captured from Golden-Ray-BOM itself, so a
difference here is a defect in the port rather than a judgement call.
"""

from __future__ import annotations

import gzip
import json
from pathlib import Path

import pytest

from engines.string_config import WARNINGS, string_config, upgrade_string_config

CORPUS = Path(__file__).resolve().parents[2] / "bom" / "tests" / "parity" / "source" / "corpus_string.json.gz"

#: A real pair from the catalog: a 540 W panel on a 3 kW single-phase string inverter.
PANEL = {"voc": 49.6, "vmp": 41.5, "isc": 13.9, "temp_coeff_voc": -0.25, "watt": 540, "max_sys_voltage": 1500}
INVERTER = {
    "mppt_count": 2,
    "mppt_voltage_min": 80,
    "mppt_voltage_max": 550,
    "max_input_voltage": 600,
    "max_input_current": 13.5,
    "max_strings_per_mppt": 1,
}


def _cases(engine: str) -> list[dict]:
    with gzip.open(CORPUS, "rt", encoding="utf-8") as handle:
        return [case for case in json.load(handle)["cases"] if case["engine"] == engine and case["result"]]


def _from_source(result: dict) -> tuple[dict, dict]:
    """The captured panel/inverter rows, in this engine's field names."""
    panel, inverter = result.get("panel") or {}, result.get("inverter") or {}
    return (
        {
            "voc": panel.get("voc"),
            "vmp": panel.get("vmp"),
            "isc": panel.get("isc"),
            "temp_coeff_voc": panel.get("tempCoeffVoc"),
            "watt": panel.get("watt"),
            "max_sys_voltage": panel.get("maxSysVoltage"),
        },
        {
            "mppt_count": inverter.get("mpptCount"),
            "mppt_voltage_min": inverter.get("mpptVoltageMin"),
            "mppt_voltage_max": inverter.get("mpptVoltageMax"),
            "max_input_voltage": inverter.get("maxInputVoltage"),
            "max_input_current": inverter.get("maxInputCurrent"),
            "max_strings_per_mppt": inverter.get("maxStringsPerMppt"),
            "is_micro": inverter.get("isMicro"),
            "optimizer_based": inverter.get("optimizerBased"),
        },
    )


class TestAgainstTheCorpus:
    def test_every_captured_layout_matches(self):
        """Layout and findings, case for case, against the tool's own output."""
        cases = _cases("computeStringConfig")
        assert cases, "corpus_string is empty"
        problems = []
        for case in cases:
            want = case["result"]
            panel, inverter = _from_source(want)
            got = string_config(panel=panel, inverter=inverter, panels=want.get("totalPanels") or 0)
            if got is None:
                problems.append(f"{case['id']}: returned None")
                continue
            if want.get("isMicro"):
                if not got.get("is_micro"):
                    problems.append(f"{case['id']}: micro-inverter not recognised")
                continue
            for name, mine, theirs in (
                ("panels_per_string", got["panels_per_string"], want.get("panelsPerString")),
                ("total_strings", got["total_strings"], want.get("totalStrings")),
                ("is_valid", got["is_valid"], want.get("isValid")),
                (
                    "warnings",
                    sorted(w["code"] for w in got["warnings"]),
                    sorted(w["code"] for w in (want.get("warnings") or [])),
                ),
            ):
                if mine != theirs:
                    problems.append(f"{case['id']}: {name} = {mine!r}, expected {theirs!r}")
        assert not problems, f"{len(problems)} differences:\n" + "\n".join(f"  {p}" for p in problems[:12])

    def test_every_captured_upgrade_layout_matches(self):
        for case in _cases("computeUpgradeStringConfig"):
            want = case["result"]
            panel, inverter = _from_source(want)
            got = upgrade_string_config(
                panel=panel,
                inverter=inverter,
                from_kw=float(case["params"].get("upgradeFromKw", 3)),
                new_panels=want.get("newPanels") or 0,
            )
            if got is None:
                continue
            assert got["panels_per_string"] == want.get("panelsPerString"), case["id"]
            assert got["existing_panels"] == want.get("existingPanels"), case["id"]
            assert sorted(w["code"] for w in got["warnings"]) == sorted(w["code"] for w in (want.get("warnings") or [])), case["id"]


class TestTheClamp:
    """Why the voltage criticals almost never fire — worth pinning, because it is not obvious.

    ``panels_per_string = max(min_per_string, min(max_per_string, optimal))`` floors the
    string at ``ceil(mppt_min / vmp_hot)``, which is by definition just long enough to
    clear the MPPT window. So ``VMP_BELOW_MPPT_MIN`` is **unreachable** on this path: the
    clamp has already fixed the thing it would report.

    The same clamp is why an impossible inverter surfaces as ``VOC_EXCEEDS_MAX`` rather
    than a window complaint — forced long enough to track, the string then overvolts. That
    is the honest finding, and it is the one an installer can act on.

    Confirmed against the corpus: across every captured case the source emits only
    ``ISC_EXCEEDS_MAX``, ``UNEVEN_STRINGS``, ``ISC_HIGH``, ``EXIST_STRINGS_HIGH`` and
    ``NEW_STRINGS_HIGH``. The five voltage/string codes are defined, faithful to the
    source, and dormant on this catalog — which is the right outcome: they are guards, not
    expected output.
    """

    def test_the_window_floor_is_always_cleared(self):
        """Whatever the window, the chosen string reaches it — so the critical cannot fire."""
        for mppt_min in (80, 200, 400, 900):
            result = string_config(
                panel=PANEL,
                inverter={**INVERTER, "mppt_voltage_min": mppt_min, "mppt_voltage_max": mppt_min + 400, "max_input_voltage": 5000},
                panels=12,
            )
            assert result["string_vmp_hot"] >= mppt_min, mppt_min
            assert "VMP_BELOW_MPPT_MIN" not in {w["code"] for w in result["warnings"]}

    def test_an_inverter_that_cannot_hold_the_string_reports_the_overvolt(self):
        """Forced long enough to track, the string overvolts — and that is reported."""
        result = string_config(
            panel=PANEL,
            inverter={**INVERTER, "mppt_voltage_min": 900, "mppt_voltage_max": 1000, "max_input_voltage": 600},
            panels=6,
        )
        assert "VOC_EXCEEDS_MAX" in {w["code"] for w in result["warnings"]}
        assert result["is_valid"] is False, "a layout that can destroy the inverter must not read as installable"

    def test_too_many_strings_for_the_available_inputs_is_critical(self):
        result = string_config(
            panel=PANEL,
            inverter={**INVERTER, "mppt_count": 1, "max_strings_per_mppt": 1, "max_input_voltage": 600},
            panels=40,
        )
        codes = {w["code"] for w in result["warnings"]}
        assert "STRINGS_EXCEED_MPPT" in codes
        assert result["is_valid"] is False

    def test_an_uneven_last_string_is_information_not_a_blocker(self):
        """Worth telling the installer, but the system still works."""
        result = string_config(panel=PANEL, inverter={**INVERTER, "max_strings_per_mppt": 4}, panels=13)
        uneven = next((w for w in result["warnings"] if w["code"] == "UNEVEN_STRINGS"), None)
        if uneven is not None:
            assert uneven["severity"] == "info"

    def test_every_warning_code_has_a_declared_severity(self):
        """So the API and the Studio cannot disagree about what blocks an install."""
        assert set(WARNINGS) >= {
            "VOC_EXCEEDS_MAX", "VMP_BELOW_MPPT_MIN", "ISC_EXCEEDS_MAX", "STRINGS_EXCEED_MPPT",
            "VMP_ABOVE_MPPT_MAX", "UNEVEN_STRINGS", "SYSTEM_VOLTAGE_EXCEEDED",
        }
        assert set(WARNINGS.values()) <= {"critical", "warning", "info"}


class TestWhatCannotBeComputed:
    @pytest.mark.parametrize(
        "panel,inverter",
        [
            ({}, INVERTER),
            (PANEL, {}),
            ({**PANEL, "voc": None}, INVERTER),
            (PANEL, {**INVERTER, "mppt_count": None}),
        ],
    )
    def test_missing_specs_give_nothing_rather_than_a_guess(self, panel, inverter):
        """A fabricated layout is worse than an absent one — it would be installed."""
        assert string_config(panel=panel, inverter=inverter, panels=6) is None

    def test_a_micro_inverter_has_no_strings(self):
        result = string_config(panel=PANEL, inverter={**INVERTER, "is_micro": True}, panels=9)
        assert result["is_micro"] is True and result["micro_count"] == 9
        assert result["warnings"] == []

    def test_an_optimiser_is_limited_by_count_not_voltage(self):
        result = string_config(panel=PANEL, inverter={**INVERTER, "optimizer_based": True}, panels=9)
        assert result["is_optimizer"] is True and result["max_string_length"] == 600
