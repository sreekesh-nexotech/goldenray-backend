#!/usr/bin/env python3
"""Capture the legacy Site Inspection V2 readiness, field-completion and equipment-status outputs.

Writes the scenarios of ``engines/tests/inspection_cases.py`` as legacy rows, runs the real legacy TypeScript through
``si_legacy_runner.mjs`` (Node ≥ 22.18, type stripping; the legacy ``./db`` import is replaced by a read-only fake)
and stores ``engines/tests/golden/si_legacy.json``, which ``engines/tests/test_inspection_parity.py`` compares with the
v4 engines.

    python scripts/parity/capture_si_legacy.py --si-root /path/to/site-inspection-v2 [--node /opt/node22/bin/node]

Not part of CI: the legacy source lives outside this repository. Re-run it when the scenario table changes.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import subprocess
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))

from engines.tests import inspection_cases as cases  # noqa: E402

RUNNER = Path(__file__).with_name("si_legacy_runner.mjs")
DEFAULT_OUT = ROOT / "engines" / "tests" / "golden" / "si_legacy.json"
SOURCES = ("lib/site-inspection.ts", "lib/equipment-assessment.ts")


def capture(si_root: Path, node: str) -> dict:
    payload = {
        "scenarios": {scenario.id: scenario.legacy_input() for scenario in cases.SCENARIOS},
        "result_sets": cases.RESULT_SETS,
    }
    with tempfile.TemporaryDirectory() as scratch:
        source = Path(scratch) / "input.json"
        target = Path(scratch) / "output.json"
        source.write_text(json.dumps(payload), encoding="utf-8")
        subprocess.run([node, str(RUNNER), str(si_root), str(source), str(target)], check=True)
        output = json.loads(target.read_text(encoding="utf-8"))
    output["source"] = {name: hashlib.sha256((si_root / name).read_bytes()).hexdigest() for name in SOURCES}
    return output


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--si-root", type=Path, required=True)
    parser.add_argument("--node", default="node")
    parser.add_argument("--out", type=Path, default=DEFAULT_OUT)
    args = parser.parse_args(argv)
    data = capture(args.si_root, args.node)
    args.out.parent.mkdir(parents=True, exist_ok=True)
    args.out.write_text(json.dumps(data, indent=1, sort_keys=True) + "\n", encoding="utf-8")
    print(f"{len(data['readiness'])} scenarios, {len(data['statuses'])} result sets -> {args.out}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
