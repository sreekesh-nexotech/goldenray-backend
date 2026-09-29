"""Build ``admin-localstorage.json``: the ``admin`` browser profile's export of the Purchase Agreement page.

No real export exists in the reference estate. The records are the page's own (``agreements/tests/fixtures/pa/
flarize_agr.json``, recorded from the page by ``export_pa_records.mjs``; personal data synthetic/masked), arranged the way
a second profile holds them — a full ``localStorage`` dump whose values are JSON strings:

* ``flarize_agr``: the page's built-in demo records (``seed()`` writes them into every fresh profile) and one real
  record also present in the ``crs`` export (a record id in both profiles);
* ``flarize_trash``: one record the page moved to its bin.

The ``crs`` export is ``flarize_agr.json`` itself (the bare array shape). Re-run::

    python migrations_tools/tests/fixtures/ops/pa/build_admin_export.py
"""

import json
from pathlib import Path

ROOT = Path(__file__).resolve().parents[5]
HERE = Path(__file__).resolve().parent

records = json.loads((ROOT / "agreements/tests/fixtures/pa/flarize_agr.json").read_text(encoding="utf-8"))
by_id = {record["id"]: record for record in records}
agreements = [by_id["agr_1789909200000"], *(by_id[key] for key in ("a1", "a2", "a3", "a4", "a5"))]
trash = [by_id["agr_1789912800000"]]
dump = {"flarize_agr": json.dumps(agreements, ensure_ascii=False), "flarize_trash": json.dumps(trash, ensure_ascii=False), "flarize_user": "admin", "flarize_last": "1789930000000"}
(HERE / "admin-localstorage.json").write_text(json.dumps(dump, indent=1, ensure_ascii=False) + "\n", encoding="utf-8")
