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
    # Phase 1. The source hardcodes 9%; the platform derives the composite rate from
    # pricing_cost_config (0.70 x 5% + 0.30 x 18% = 8.9%) through
    # engines.money.resolve_gst_regime, which is the same scheme audited and stamped into
    # PriceRelease.payload["gst"]. Hardcoding 9 would fork the platform's single source of
    # GST truth to reproduce a rounding. The delta is ~0.1pp, always in the customer's
    # favour.
    "gst_effective_rate": "composite 8.9% from cost config, not the source's rounded 9%",
}


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


def differences(expected: dict, actual: dict, *, path: str = "") -> list[str]:
    """Every leaf where ``actual`` departs from ``expected``, as readable paths.

    Reports the whole set rather than failing on the first, because a pricing change
    usually moves a dozen related numbers at once and seeing them together is what tells
    you which stage of the ladder moved.
    """
    out: list[str] = []
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
