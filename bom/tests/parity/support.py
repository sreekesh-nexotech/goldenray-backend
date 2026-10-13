"""Load the Golden-Ray-BOM corpus and compare the platform's quote against it.

The corpus under ``source/`` was captured by ``capture_source.mjs`` from the tool's own
JavaScript (see that file and ``extract_source.mjs``). Golden-Ray-BOM is the behavioural
source of truth for the BOM calculator, so a difference is a platform defect unless it is
listed in :data:`APPROVED_DIFFERENCES`.

Comparison is on **serialised JSON**, like ``calculators`` and ``emi`` do it: ``13122.0``
is not ``13122``. A parity port that silently changes a float to an int has changed the
response contract, and every downstream consumer — the public website quote, the
quotations pipeline, ``packs`` — reads these numbers.
"""

from __future__ import annotations

import gzip
import json
from pathlib import Path

HERE = Path(__file__).resolve().parent
VARIANT = "source"

#: Corpus files and the source engines each one may replay. ``string`` carries both
#: string engines, since the upgrade variant is the same feature on the upgrade path.
CORPORA = {
    "standard": {"computeBom"},
    "standard_fu": {"computeBom"},
    "micro": {"computeBom"},
    "upgrade": {"computeUpgradeBom"},
    "string": {"computeStringConfig", "computeUpgradeStringConfig"},
    "bill": {"computeBillCalc"},
    "discount": {"computeDiscountCalc"},
}

#: Differences from the source that are intended, with the reason each is accepted.
#:
#: Keep this list short and specific. Every entry is a place the platform deliberately
#: does NOT match Golden-Ray-BOM, so each one needs a reason a reviewer can check —
#: "rounding" is not a reason, "the split is authoritative and derives 8.9%" is.
APPROVED_DIFFERENCES: dict[str, str] = {
    # NOTE: the GST rate is NOT a difference. The platform states 9 % outright through
    # gst.quote_regime.rate_pct rather than deriving 8.9 % from the 70/30 split, because
    # Golden Ray invoices solar at a flat 9 % and the quote has to show the rate actually
    # charged. engines.money still resolves it, as the FLAT regime.
    #
    # The source gets JSON-native numbers (13122 is an int, 1.7 a float); the platform
    # reads Decimal columns and emits floats throughout (13122.0). The retired golden file
    # pins that float shape as the public response contract, and `quotations` snapshots
    # and `packs` read it, so matching the source's mixed types would be a breaking change
    # for a cosmetic gain. Values are compared numerically instead.
    "number_types": "platform emits floats for prices; the source emits JSON-native ints/floats",
    # The source reports its material and ladder totals unrounded (123537.3), carrying a
    # fraction that comes from per-watt panel pricing. The platform's response contract
    # declares them as integers -- BomQuoteTotalsSerializer uses IntegerField and the
    # retired golden file shows ints -- so they are rounded for the response while the
    # ladder still compounds off the exact value internally. Totals are therefore
    # compared to the rupee.
    "totals_are_whole_rupees": "platform rounds totals to int per its contract; the source keeps the fraction",
}

#: Response paths where only the numeric value is compared, not its JSON type. Entries
#: here are covered by ``APPROVED_DIFFERENCES["number_types"]``.
NUMERIC_ONLY_FIELDS = frozenset({"unit_price", "amount", "qty", "market_rate", "discount_amt", "subsidy_amt"})

#: Response paths compared to the nearest rupee rather than exactly, covered by
#: ``APPROVED_DIFFERENCES["totals_are_whole_rupees"]``.
WHOLE_RUPEE_FIELDS = frozenset(
    {"material_ex_gst", "material_with_gst", "pre_gst_subtotal", "subtotal", "grand_total", "customer_price", "final_price", "price_after_subsidy"}
)


def _read_gz(name: str):
    with gzip.open(HERE / VARIANT / name, "rt", encoding="utf-8") as handle:
        return json.load(handle)


def manifest() -> dict:
    return json.loads((HERE / VARIANT / "manifest.json").read_text())


def load_corpus(key: str) -> list[dict]:
    """The cases of one corpus file, in capture order."""
    return _read_gz(f"corpus_{key}.json.gz")["cases"]


def catalog() -> dict:
    """The exact ``catalog.json`` the corpus was captured against."""
    return _read_gz("catalog.json.gz")


def dumped(value) -> str:
    """Serialise for comparison: key order preserved, number types significant."""
    return json.dumps(value, ensure_ascii=False, sort_keys=False)


def _leaf_name(path: str) -> str:
    """The field name a path ends in, ignoring any list index (``lines[3].amount``)."""
    return path.rsplit(".", 1)[-1].split("[", 1)[0]


def differences(expected: dict, actual: dict, *, path: str = "") -> list[str]:
    """Every leaf where ``actual`` departs from ``expected``, as readable paths.

    Reports the whole set rather than failing on the first, because a pricing change
    usually moves a dozen related numbers at once and seeing them together is what tells
    you which stage of the ladder moved.

    Comparison is on serialised JSON — so ``13122.0`` is not ``13122`` — except on the
    price fields in :data:`NUMERIC_ONLY_FIELDS`, where the platform's float shape is an
    approved difference and only the value is checked.
    """
    out: list[str] = []
    leaf = _leaf_name(path)
    if isinstance(expected, int | float) and isinstance(actual, int | float) and not isinstance(expected, bool):
        if leaf in NUMERIC_ONLY_FIELDS:
            if float(expected) != float(actual):
                out.append(f"{path}: {actual}, expected {expected}")
            return out
        if leaf in WHOLE_RUPEE_FIELDS:
            if abs(float(expected) - float(actual)) >= 1:
                out.append(f"{path}: {actual}, expected {expected} (beyond a rupee)")
            return out
    if isinstance(expected, dict) and isinstance(actual, dict):
        for key in expected.keys() | actual.keys():
            where = f"{path}.{key}" if path else str(key)
            if key not in expected:
                out.append(f"{where}: unexpected key = {dumped(actual[key])}")
            elif key not in actual:
                out.append(f"{where}: missing key, expected {dumped(expected[key])}")
            else:
                out.extend(differences(expected[key], actual[key], path=where))
        return out
    if isinstance(expected, list) and isinstance(actual, list):
        if len(expected) != len(actual):
            out.append(f"{path}: length {len(actual)}, expected {len(expected)}")
        for index, (want, got) in enumerate(zip(expected, actual)):
            out.extend(differences(want, got, path=f"{path}[{index}]"))
        return out
    if dumped(expected) != dumped(actual):
        out.append(f"{path}: {dumped(actual)}, expected {dumped(expected)}")
    return out
