"""Golden parity: every case recorded from the real Flarize JavaScript must be reproduced exactly by the Python port.

Regenerate the goldens with ``node engines/tests/golden/generate_commercial.mjs`` (see docs/decisions/engines-commercial.md).
"""

import hashlib
import os
from pathlib import Path

import pytest

from engines._jscompat import JsError
from engines.tests.golden_harness import MODULES, assert_error_same, assert_same, error_dict, expected_result, load_fixtures, load_golden, normalize, run_case

CASES = [(module, case) for module in MODULES for case in load_golden(module)["cases"]]
FLARIZE_ROOT = Path(os.environ.get("FLARIZE_ROOT", "/home/user/flarize-main/flarize"))


def test_golden_suite_is_large_enough():
    assert len(CASES) >= 300
    assert {module for module, _ in CASES} == set(MODULES)


def test_goldens_share_one_provenance():
    sources = load_fixtures()["sources"]
    assert all(load_golden(module)["sources"] == sources for module in MODULES)
    assert "server-bom-builder.js" in sources and "data/pack-config.json" in sources


@pytest.mark.skipif(not FLARIZE_ROOT.is_dir(), reason="the Flarize sources are not on this machine (CI replays the committed goldens)")
def test_goldens_were_generated_from_the_current_flarize_sources():
    """Stale goldens fail here: regenerate with ``node engines/tests/golden/generate_commercial.mjs``."""
    for relative, digest in load_fixtures()["sources"].items():
        assert hashlib.sha256((FLARIZE_ROOT / relative).read_bytes()).hexdigest() == digest, relative


@pytest.mark.parametrize(("module", "case"), CASES, ids=[f"{m}:{c['id']}" for m, c in CASES])
def test_golden_case(module, case):
    try:
        actual = run_case(module, case)
    except JsError as error:
        assert "error" in case, f"Python raised {error.js_name}({error.code}): {error.message}; JS returned a result"
        assert_error_same(case["error"], error_dict(error))
        return
    assert "error" not in case, f"JS threw {case['error']}; Python returned {actual!r}"
    assert_same(expected_result(case), normalize(actual))
