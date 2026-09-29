#!/usr/bin/env python3
"""Capture the eSSL v3 attendance engine's output for every case of ``engines/tests/attendance_cases.py``.

Runs the real legacy ``backend/app/services/processing.py`` (read-only; its SQLAlchemy and model imports are stubbed,
the functions used here only read attributes) and writes ``engines/tests/golden/essl_v3_attendance.json``, which
``engines/tests/test_attendance_parity.py`` compares with the v4 engine.

    python scripts/parity/capture_essl_v3.py --essl-root /path/to/essl-webap-main [--out engines/tests/golden/essl_v3_attendance.json]

Not part of CI: the legacy source lives outside this repository. Re-run it when the case table changes.
"""

from __future__ import annotations

import argparse
import enum
import hashlib
import importlib.util
import json
import sys
import types
from datetime import date, datetime
from pathlib import Path
from types import SimpleNamespace

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))

from engines.tests import attendance_cases as cases  # noqa: E402

DEFAULT_OUT = ROOT / "engines" / "tests" / "golden" / "essl_v3_attendance.json"


def _stub_legacy_imports() -> None:
    """Just enough of ``sqlalchemy`` and ``app.models`` for ``processing.py`` to import."""

    class AttendanceStatus(str, enum.Enum):
        PRESENT = "PRESENT"
        LATE = "LATE"
        ABSENT = "ABSENT"
        HALF_DAY = "HALF_DAY"
        WEEKLY_OFF = "WEEKLY_OFF"
        HOLIDAY = "HOLIDAY"
        ON_LEAVE = "ON_LEAVE"

    class LeaveStatus(str, enum.Enum):
        PENDING = "PENDING"
        APPROVED = "APPROVED"
        REJECTED = "REJECTED"
        CANCELLED = "CANCELLED"

    def module(name: str, **attrs) -> types.ModuleType:
        mod = types.ModuleType(name)
        mod.__dict__.update(attrs)
        sys.modules[name] = mod
        return mod

    placeholder = type("Model", (), {})
    module("sqlalchemy", select=lambda *a, **k: None)
    module("sqlalchemy.orm", Session=placeholder)
    module("app")
    module("app.models")
    module("app.models.attendance", Attendance=placeholder, AttendanceRaw=placeholder, AttendanceStatus=AttendanceStatus)
    module("app.models.device", DeviceUser=placeholder)
    module(
        "app.models.org",
        AttendanceRule=placeholder,
        Employee=placeholder,
        Holiday=placeholder,
        LeaveRecord=placeholder,
        LeaveStatus=LeaveStatus,
        Office=placeholder,
        Shift=placeholder,
    )


def _load_processing(essl_root: Path):
    path = essl_root / "backend" / "app" / "services" / "processing.py"
    _stub_legacy_imports()
    spec = importlib.util.spec_from_file_location("essl_processing_v3", path)
    processing = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = processing  # dataclasses resolve annotations through sys.modules
    spec.loader.exec_module(processing)
    return processing, hashlib.sha256(path.read_bytes()).hexdigest()


def _v3_shift(overrides: dict | None):
    if overrides is None:
        return None
    values = {**cases.V3_SHIFT_DEFAULTS, **{k: v for k, v in overrides.items() if k in cases.V3_SHIFT_DEFAULTS or k in ("start_time", "end_time")}}
    values["working_days"] = list(values["working_days"])
    values["weekly_off_days"] = list(values["weekly_off_days"])
    return SimpleNamespace(id=1, code="CASE", **values)


def _v3_rules(processing, rules: tuple, shift, work_date: date):
    rule_rows = [
        SimpleNamespace(
            effective_from=rule.get("effective_from"),
            shift_id=1 if rule["scope"] == "shift" else None,
            office_id=1 if rule["scope"] == "office" else None,
            rules=rule["rules"],
        )
        for rule in rules
        if rule.get("is_active", True)  # v3 load_rules() filters active rules before rules_for()
    ]
    return processing.rules_for(rule_rows, SimpleNamespace(office_id=1), shift, work_date)


def _json(value):
    if isinstance(value, (datetime, date)):
        return value.isoformat()
    if isinstance(value, enum.Enum):
        return value.value
    if isinstance(value, (list, tuple)):
        return [_json(item) for item in value]
    return value


def capture(essl_root: Path) -> dict:
    processing, digest = _load_processing(essl_root)
    out: dict = {
        "source": {
            "file": "essl-webap-main/backend/app/services/processing.py",
            "sha256": digest,
            "processing_version": processing.PROCESSING_VERSION,
        },
        "cases": {},
        "deadlines": {},
        "attributions": {},
        "break": {},
    }
    for case in cases.CASES:
        shift = _v3_shift(case.shift)
        punches = [SimpleNamespace(id=raw_id, punch_time=moment) for raw_id, moment, _device in case.punch_list()]
        ctx = processing.DayContext(**case.context)
        rules = _v3_rules(processing, case.rules, shift, case.day)
        result = processing.compute_day(case.day, punches, shift, ctx, rules)
        out["cases"][case.id] = {name: _json(getattr(result, name)) for name in cases.COMPARED_FIELDS}
    for case in cases.DEADLINES:
        shift = _v3_shift(case.shift)
        out["deadlines"][case.id] = _json(processing.half_day_deadline(case.day, shift, _v3_rules(processing, case.rules, shift, case.day)))
    for case in cases.ATTRIBUTIONS:
        out["attributions"][case.id] = _json(processing.attribute_work_date(case.wall_clock, _v3_shift(case.shift)))
    shift = _v3_shift(cases.GEN)
    out["break"] = [processing._deduct_break(gross, shift) for gross in cases.BREAK_GROSS_RANGE]  # working minutes, index = gross
    return out


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--essl-root", type=Path, required=True)
    parser.add_argument("--out", type=Path, default=DEFAULT_OUT)
    args = parser.parse_args(argv)
    data = capture(args.essl_root)
    args.out.parent.mkdir(parents=True, exist_ok=True)
    args.out.write_text(json.dumps(data, indent=1, sort_keys=True) + "\n", encoding="utf-8")
    print(f"{len(data['cases'])} cases, {len(data['deadlines'])} deadlines, {len(data['attributions'])} attributions -> {args.out}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
