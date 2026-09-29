"""Sources of the operational imports, built from committed fixtures (all exported read-only from the legacy sources and
masked by the packages that own them; migration-ops adds only what nobody had committed, see ``fixtures/ops/``)."""

from __future__ import annotations

import json
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
OPS = Path(__file__).resolve().parent / "fixtures" / "ops"
SI_DB = OPS / "si" / "flarize-site-inspection.db"
PA_CRS = ROOT / "agreements/tests/fixtures/pa/flarize_agr.json"
PA_ADMIN = OPS / "pa" / "admin-localstorage.json"
PA_CATALOG = ROOT / "agreements/tests/fixtures/pa/catalog.json"
ESSL_SEEDED = OPS / "essl" / "essl_seeded.json"
ESSL_CAPTURED = ROOT / "attendance/tests/legacy/essl_attendance_tables.json"
FLARIZE_MEDIA = OPS / "flarize"  # holds cms-assets/ (the importer reads "cms-assets/<file>" under --media-root)


def _json(path: Path):
    return json.loads(path.read_text(encoding="utf-8"))


def flarize_tables() -> dict[str, list]:
    """The Flarize ``data/`` folder as ``{file: [document]}``."""
    commercial = _json(ROOT / "engines/tests/golden/fixtures/flarize_commercial.json")
    pricing = ROOT / "pricing/tests/fixtures/legacy"
    procurement = ROOT / "procurement/tests/fixtures/legacy"
    quotations = ROOT / "quotations/tests/fixtures/flarize"
    documents = {
        "catalog.json": commercial["catalog"],
        "battery-master.json": {"batteries": commercial["batteryMaster"]},
        "pack-config.json": commercial["packStore"],
        "packages.proposed.json": commercial["registry"],
        "cost-config.json": _json(pricing / "flarize_cost-config.json"),
        "project-rate-card.json": _json(pricing / "flarize_project-rate-card.json"),
        "quotation-policy.json": _json(pricing / "flarize_quotation-policy.json"),
        **{f"{name}-config.json": _json(pricing / f"flarize_{name}-config.json") for name in ("energy", "savings", "subsidy", "finance")},
        "procurement-state.json": _json(procurement / "flarize_procurement-state.json"),
        "procurement-price-master.json": _json(procurement / "flarize_procurement-price-master.json"),
        "commercial-history.json": _json(procurement / "flarize_commercial-history.json"),
        **{path.name: _json(path) for path in sorted(quotations.glob("*.json"))},
        "customers.json": _json(ROOT / "customers/tests/fixtures/flarize/customers.json"),
        "workspace-state.json": _json(ROOT / "projects/tests/fixtures/flarize_workspace_state.json"),
        "bom-state.json": _json(ROOT / "projects/tests/fixtures/flarize_bom_state.json"),
        "users.json": _json(OPS / "flarize" / "users.json"),
        "company-profile.json": _json(OPS / "flarize" / "company-profile.json"),
        "cms-state.json": _json(OPS / "flarize" / "cms-state.json"),
    }
    return {name: [value] for name, value in documents.items()}


def write_fixture(path: Path, tables: dict) -> Path:
    path.write_text(json.dumps({"tables": tables}, ensure_ascii=False), encoding="utf-8")
    return path
