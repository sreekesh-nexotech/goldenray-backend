"""The Purchase Agreement import plan — PLAN §7.5 "Purchase Agreement", run **before** ``import_si``.

The page kept its records only in the browser (``localStorage['flarize_agr']``) of two profiles, ``crs`` and ``admin``;
each profile's export is one JSON file (:func:`read_export`). The source is built in memory
(:func:`export_tables`): ``flarize_agr:<profile>`` and ``flarize_trash:<profile>`` (records the page moved to its bin),
``catalog`` (the Upstash ``/api/products`` catalog, when given) and ``kseb`` (that catalog's KSEB fee list, else the
page's built-in ``KSEB_FEES``).

Steps: the KSEB registration fees → ``pricing_statutory_fee`` (``pricing.services.legacy_import.import_pa_kseb_fees``),
the catalog comparison (``agreements.services.legacy_import.report_pa_catalog``: listed, nothing created), then each
profile's records → ISSUED ``legacy`` agreements (``import_pa_agreements``) whose uid is
``uuid5(SI_AGREEMENT_NAMESPACE, "PA:<record id>")`` — the key a legacy PA site inspection carries
(``site_inspections.services.legacy_import.agreement_uid``, the default of ``import_pa_agreements``' ``uid_for``
hook), so ``import_si`` links them.
"""

from __future__ import annotations

import json
from pathlib import Path

from agreements.services import legacy_import as agreements_import
from core.models import LegacyMap
from migrations_tools.services.runner import Context, Plan, Step
from migrations_tools.services.source import SourceError
from pricing.services import legacy_import as pricing_import

PA = LegacyMap.SourceSystem.PA
PROFILES = ("crs", "admin")
# The page's built-in KSEB fee options (index.html ``KSEB_FEES``), used when no Upstash catalog export is given.
PAGE_KSEB_FEES = (
    {"v": "5400", "l": "3 KW — ₹ 5,400"},
    {"v": "7800", "l": "5 KW — ₹ 7,800"},
    {"v": "11240", "l": "8 KW — ₹ 11,240"},
    {"v": "13600", "l": "10 KW — ₹ 13,600"},
    {"v": "15960", "l": "12 KW — ₹ 15,960"},
    {"v": "19500", "l": "15 KW — ₹ 19,500"},
)


def _value(value):
    """A localStorage value is a JSON string; an export tool may already have parsed it."""
    if isinstance(value, str):
        try:
            return json.loads(value)
        except ValueError as exc:
            raise SourceError(f"a localStorage value is not JSON ({exc})") from exc
    return value


def read_export(path: str | Path) -> tuple[list[dict], list[dict]]:
    """One profile's export → ``(flarize_agr records, flarize_trash records)``.

    Accepted shapes: the ``flarize_agr`` array itself, or a localStorage dump ``{"flarize_agr": …, "flarize_trash": …}``
    (values as JSON strings, as ``localStorage`` holds them, or already parsed)."""
    try:
        data = json.loads(Path(path).read_text(encoding="utf-8"))
    except (OSError, ValueError) as exc:
        raise SourceError(f"{path}: not a readable JSON export ({exc.__class__.__name__}).") from exc
    if isinstance(data, list):
        return [row for row in data if isinstance(row, dict)], []
    if isinstance(data, dict) and "flarize_agr" in data:
        records, trash = _value(data.get("flarize_agr")) or [], _value(data.get("flarize_trash")) or []
        if isinstance(records, list) and isinstance(trash, list):
            return [row for row in records if isinstance(row, dict)], [row for row in trash if isinstance(row, dict)]
    raise SourceError(f"{path}: expected the flarize_agr array or a localStorage dump with a 'flarize_agr' key.")


def kseb_rows(catalog: dict | None) -> list[dict]:
    """KSEB fee entries as rows: the catalog's ``"<label>|<fee>"`` strings, else the page's built-in options."""
    if catalog and catalog.get("kseb"):
        return [{"entry": str(item.get("value") if isinstance(item, dict) else item)} for item in catalog["kseb"]]
    return [dict(item) for item in PAGE_KSEB_FEES]


def export_tables(exports: dict[str, str | Path], catalog_path: str | Path | None = None) -> dict[str, list[dict]]:
    tables: dict[str, list[dict]] = {}
    for profile, path in exports.items():
        records, trash = read_export(path)
        tables[f"flarize_agr:{profile}"] = records
        tables[f"flarize_trash:{profile}"] = trash
    catalog = None
    if catalog_path:
        try:
            catalog = json.loads(Path(catalog_path).read_text(encoding="utf-8"))
        except (OSError, ValueError) as exc:
            raise SourceError(f"{catalog_path}: not a readable catalog export ({exc.__class__.__name__}).") from exc
        tables["catalog"] = [catalog]
    tables["kseb"] = kseb_rows(catalog)
    return tables


def _entries(rows: list[dict]) -> list:
    return [row["entry"] if "entry" in row else row for row in rows]


def _kseb(rows, ctx: Context) -> dict:
    return {"kseb": pricing_import.import_pa_kseb_fees(_entries(rows["kseb"]), user=ctx.user)}


def _catalog(rows, ctx: Context) -> dict:
    if not rows["catalog"]:
        return {"catalog": {"created": 0, "updated": 0, "skipped": 0, "violations": []}}
    return {"catalog": agreements_import.report_pa_catalog(rows["catalog"][0])}


def _trash_report(profile: str, trash: list[dict]) -> dict:
    """Records the page's bin held are not migrated (the page deleted them); they are listed for the client."""
    violations = [
        {
            "source_table": "flarize_trash",
            "source_id": f"{profile}/{row.get('id')}",
            "code": "trashed_record_not_migrated",
            "message": f"{row.get('typeName') or 'record'} of {row.get('customerName') or '?'} is in the page's bin; not migrated.",
        }
        for row in trash
    ]
    return {"created": 0, "updated": 0, "skipped": len(trash), "violations": violations}


def _profile_step(profile: str):
    def run(rows, ctx: Context) -> dict:
        return {
            f"flarize_agr:{profile}": agreements_import.import_pa_agreements(rows[f"flarize_agr:{profile}"], profile=profile, user=ctx.user),
            f"flarize_trash:{profile}": _trash_report(profile, rows[f"flarize_trash:{profile}"]),
        }

    return run


def _record_keys(profile: str):
    return lambda rows: [(agreements_import.SOURCE_TABLE, f"{profile}/{row['id']}") for row in rows if row.get("id")]


def _kseb_keys(rows):
    keys = []
    for row in rows:
        label = str(row["entry"]).rpartition("|")[0] if "entry" in row else str(row.get("l") or "")
        if label:
            keys.append(("kseb", label))
    return keys


PLAN = Plan(
    source_system=PA,
    steps=(
        Step("pa.kseb_fees", "masters", ("kseb",), _kseb, "KSEB registration fee bands → pricing_statutory_fee"),
        Step("pa.catalog_report", "masters", ("catalog",), _catalog, "Upstash catalog vs platform masters (listed, nothing created)"),
        *(
            Step(
                f"pa.agreements_{profile}",
                "transactional",
                (f"flarize_agr:{profile}", f"flarize_trash:{profile}"),
                _profile_step(profile),
                f"the {profile} profile's records → ISSUED legacy agreements",
            )
            for profile in PROFILES
        ),
    ),
    row_keys={
        "kseb": _kseb_keys,
        "catalog": lambda rows: [],
        **{f"flarize_agr:{profile}": _record_keys(profile) for profile in PROFILES},
        **{f"flarize_trash:{profile}": (lambda rows: []) for profile in PROFILES},
    },
)
