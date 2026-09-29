"""Legacy integer ids ↔ platform rows (read-only use of ``core_legacy_map``).

The old contracts expose integer ids (and the website posts some back: an EMI size, a job position). A migrated row
answers with its legacy id (``core_legacy_map.source_id``); a row created on the platform after the migration has no
legacy id and answers with ``SHIM_ID_OFFSET + <platform id>`` — above every legacy id, stable, and resolvable back
(DV row "legacy shim ids"). Nothing here writes.
"""

from __future__ import annotations

from collections.abc import Iterable

from core.models import LegacyMap

SHIM_ID_OFFSET = 10_000_000
BACKEND = LegacyMap.SourceSystem.BACKEND
CMS = LegacyMap.SourceSystem.CMS


def legacy_ids(model, pks: Iterable[int], *, system: str, table: str) -> dict[int, int]:
    """``{platform pk: legacy id}`` for every pk (one query)."""
    pks = list(pks)
    out: dict[int, int] = {}
    rows = LegacyMap.objects.filter(source_system=system, source_table=table, target_table=model._meta.db_table, target_id__in=pks).values_list("target_id", "source_id")
    for target_id, source_id in rows:
        try:
            out[target_id] = int(source_id)
        except ValueError:
            continue
    for pk in pks:
        out.setdefault(pk, SHIM_ID_OFFSET + pk)
    return out


def legacy_id(model, pk: int, *, system: str, table: str) -> int:
    return legacy_ids(model, [pk], system=system, table=table)[pk]


def resolve_pk(model, value: int, *, system: str, table: str) -> int | None:
    """The platform pk behind a legacy id (``None`` when no row has it)."""
    if value >= SHIM_ID_OFFSET:
        return value - SHIM_ID_OFFSET
    return LegacyMap.objects.filter(source_system=system, source_table=table, target_table=model._meta.db_table, source_id=str(value)).values_list("target_id", flat=True).first()
