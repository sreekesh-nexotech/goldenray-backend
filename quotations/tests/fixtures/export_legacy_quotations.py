"""Export the legacy quotation sources as masked test fixtures (run by hand; every source is read read-only).

    python quotations/tests/fixtures/export_legacy_quotations.py [FLARIZE_DATA_DIR]

Flarize (``data/``): ``quotation-content.json``, ``quotation-inclusions.json``, ``tier-display-names.json``,
``quotation-counter.json`` as they are (no personal data); ``quotation-testimonials.json`` with the homeowner names
replaced; ``quotation-branding-state.json`` with account numbers and UPI ids replaced; ``quotation-state.json`` reduced
to a representative subset (the DRAFT, quotations without, with one and with two alternative tiers, two orphan
snapshots) with every customer's name, phone, e-mail, street address and pincode replaced and the company bank
details in the frozen payloads replaced — everywhere they occur (records, payloads, alternative payloads), so the
subset is self-consistent. Main backend (``legacy_goldenapp``, read-only ``SELECT``): ``bom_quotationtestimonial``
with the homeowner names replaced, ``sent_quotes`` (names and phones replaced).
"""

from __future__ import annotations

import hashlib
import json
import subprocess
import sys
from pathlib import Path

HERE = Path(__file__).resolve().parent
FLARIZE_OUT = HERE / "flarize"
BACKEND_OUT = HERE / "backend"
SUBSET = (
    "sv-test",
    "QT-ongrid_3_value_20260830170726",
    "QT-ongrid_3_value_2026090904462529592ia",
    "QT-ongrid_5sp_base_20260910120000000l3n6",
    "QT-ongrid_3_value_202609090636284168ro7",
    "QT-ongrid_3_value_202609131520232114jwe",
    "QT-ongrid_5sp_value_20260913155254341fthx",
)
CUSTOMER_KEYS = {"name": "Customer", "customerName": "Customer", "salutationName": "Customer"}
COMPANY_SECRETS = ("bankAccountNumber", "bankAccountName", "upiId")


def _digest(value: str) -> int:
    return int(hashlib.sha256(value.encode()).hexdigest(), 16)


def mask_customer(customer: dict, label: str) -> dict:
    out = dict(customer)
    for key in ("name", "customerName", "salutationName"):
        if out.get(key):
            out[key] = f"{CUSTOMER_KEYS[key]} {label}"
    if out.get("phone"):
        out["phone"] = f"9{_digest(str(out['phone'])) % 10**9:09d}"
    if out.get("email"):
        out["email"] = f"customer{label.lower()}@example.com"
    if out.get("address"):
        out["address"] = f"Address {label}"
    if out.get("pincode"):
        out["pincode"] = "688001"
    return out


def mask_tree(value, label: str):
    """Mask every ``customer`` object and the company bank details wherever they occur in a document."""
    if isinstance(value, list):
        return [mask_tree(item, label) for item in value]
    if not isinstance(value, dict):
        return value
    out = {}
    for key, item in value.items():
        if key == "customer" and isinstance(item, dict):
            out[key] = mask_customer(mask_tree(item, label), label)
        elif key in COMPANY_SECRETS and isinstance(item, str):
            out[key] = "TEST-0000" if key != "upiId" else "test@upi"
        elif key in ("accountNumber",) and isinstance(item, str):
            out[key] = "TEST-0000"
        else:
            out[key] = mask_tree(item, label)
    return out


def export_flarize(data: Path) -> None:
    FLARIZE_OUT.mkdir(parents=True, exist_ok=True)
    for name in ("quotation-content.json", "quotation-inclusions.json", "tier-display-names.json", "quotation-counter.json"):
        (FLARIZE_OUT / name).write_text((data / name).read_text(encoding="utf-8"), encoding="utf-8")
    testimonials = json.loads((data / "quotation-testimonials.json").read_text())
    testimonials["entries"] = [{**entry, "name": f"Homeowner {index:02d}"} for index, entry in enumerate(testimonials.get("entries") or [], start=1)]
    (FLARIZE_OUT / "quotation-testimonials.json").write_text(json.dumps(testimonials, indent=1, ensure_ascii=False) + "\n")
    branding = mask_tree(json.loads((data / "quotation-branding-state.json").read_text()), "B")
    (FLARIZE_OUT / "quotation-branding-state.json").write_text(json.dumps(branding, indent=1, ensure_ascii=False) + "\n")

    state = json.loads((data / "quotation-state.json").read_text())
    quotations = {qid: state["quotations"][qid] for qid in SUBSET}
    snapshots = set()
    for record in quotations.values():
        snapshots.update(filter(None, [record.get("bomSnapshotId"), record.get("commercialSnapshotId")]))
        for option in record.get("alternativeOptions") or []:
            snapshots.update(filter(None, [option.get("bomSnapshotId"), option.get("commercialSnapshotId")]))
    referenced = set()
    for record in state["quotations"].values():
        referenced.update(filter(None, [record.get("bomSnapshotId"), record.get("commercialSnapshotId")]))
        for option in record.get("alternativeOptions") or []:
            referenced.update(filter(None, [option.get("bomSnapshotId"), option.get("commercialSnapshotId")]))
    orphans = sorted(set(state["bomSnapshots"]) - referenced)[:2]
    subset = {
        "storeVersion": state["storeVersion"],
        "bomSnapshots": {key: value for key, value in state["bomSnapshots"].items() if key in snapshots or key in orphans},
        "commercialSnapshots": {key: value for key, value in state["commercialSnapshots"].items() if key in snapshots},
        "quotations": {},
        "documents": {},
        "versionStatus": {key: value for key, value in state["versionStatus"].items() if key.split("#")[0] in quotations},
    }
    for index, (qid, record) in enumerate(quotations.items(), start=1):
        label = f"{index:02d}"
        subset["quotations"][qid] = mask_tree(record, label)
        subset["documents"][qid] = mask_tree(state["documents"].get(qid) or [], label)
    (FLARIZE_OUT / "quotation-state.json").write_text(json.dumps(subset, ensure_ascii=False) + "\n")


def _psql(sql: str) -> list[dict]:
    command = ["psql", "-h", "localhost", "-U", "postgres", "-d", "legacy_goldenapp", "-At", "-c", f"SELECT coalesce(json_agg(t), '[]') FROM ({sql}) t"]
    output = subprocess.run(command, check=True, capture_output=True, text=True, env={"PGPASSWORD": "postgres", "PATH": "/usr/bin:/bin"}).stdout
    return json.loads(output or "[]")


def export_backend() -> None:
    BACKEND_OUT.mkdir(parents=True, exist_ok=True)
    rows = _psql("SELECT * FROM bom_quotationtestimonial ORDER BY id")
    rows = [{**row, "name": f"Homeowner {row['id']:02d}"} for row in rows]
    (BACKEND_OUT / "bom_quotationtestimonial.json").write_text(json.dumps(rows, indent=1, ensure_ascii=False) + "\n")
    sent = _psql("SELECT * FROM sent_quotes ORDER BY id")
    sent = [{**row, "name": f"Customer {row['id']:02d}", "phone": f"9{_digest(str(row['phone'])) % 10**9:09d}"} for row in sent]
    (BACKEND_OUT / "sent_quotes.json").write_text(json.dumps(sent, indent=1, ensure_ascii=False) + "\n")


if __name__ == "__main__":
    export_flarize(Path(sys.argv[1] if len(sys.argv) > 1 else "/home/user/flarize-main/flarize/data"))
    export_backend()
