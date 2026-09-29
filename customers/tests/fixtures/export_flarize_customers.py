"""Export Flarize ``data/customers.json`` as a masked test fixture (run by hand; the source file is read-only).

    python customers/tests/fixtures/export_flarize_customers.py /home/user/flarize-main/flarize/data/customers.json

Names, phone numbers, e-mail addresses and street addresses are replaced deterministically (the same phone always
masks to the same number, and a valid Indian mobile stays a valid Indian mobile, so matching by phone still works);
a number that is not a valid mobile (test junk such as ``12345678901``) is kept as it is, because the importer's
handling of it is what the tests check. Every other field (ids, pincodes, bills, sources, owners, timestamps) is kept.
"""

from __future__ import annotations

import hashlib
import json
import re
import sys
from pathlib import Path

HERE = Path(__file__).resolve().parent / "flarize"
MOBILE = re.compile(r"^[6-9][0-9]{9}$")


def _digest(value: str) -> int:
    return int(hashlib.sha256(value.encode()).hexdigest(), 16)


def mask(row: dict, index: int) -> dict:
    masked = dict(row)
    masked["name"] = f"Customer {index:02d}"
    phone = (row.get("phone") or "").strip()
    if MOBILE.match(phone):
        masked["phone"] = f"9{_digest(phone) % 10**9:09d}"
    if row.get("email"):
        masked["email"] = f"customer{index:02d}@example.com"
    if row.get("address"):
        masked["address"] = f"Address {index:02d}"
    return masked


def main(source: str) -> None:
    rows = json.loads(Path(source).read_text())
    HERE.mkdir(parents=True, exist_ok=True)
    (HERE / "customers.json").write_text(json.dumps([mask(row, index) for index, row in enumerate(rows, start=1)], indent=1, ensure_ascii=False) + "\n")


if __name__ == "__main__":
    main(sys.argv[1] if len(sys.argv) > 1 else "/home/user/flarize-main/flarize/data/customers.json")
