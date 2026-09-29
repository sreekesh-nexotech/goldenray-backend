"""The business defaults an import report must spell out (``docs/decisions/business-defaults.md``).

The importers already report every affected row as a violation; this module groups them under the decision they
implement so the report hands each list to the people who asked for it (sales, engineering, HR):

* **B-2** — Flarize values win over the main backend (D-2): every changed value (``d2_flarize_wins``, both values);
* **B-3** — the approved pack configuration's market rates win over ``catalog.json`` (``pack_config_market_rate_wins``);
* **B-5** — Studio inverters stored as ``1P``/``3P`` never match a ``1P-HYB``/``3P-HYB`` slot filter: legacy matching is
  kept (byte parity), the affected components are listed (:func:`hybrid_phase_mismatches`, computed after a step that
  imports BOM data). The same filter skips every other component of such a slot's category stored as ``1P``/``3P``
  (ACDB/DCDB rows too), so the list covers the whole category, not only inverters;
* **B-12** — EMI banks with a malformed slug or logo colour are skipped and listed (``emi_bank`` violations on
  ``slug`` / ``logo_bg``).
"""

from __future__ import annotations

from collections.abc import Iterable

DEFAULTS = {
    "B-2": "Flarize values win over the main backend (D-2): values changed by the import, for sales",
    "B-3": "The approved pack configuration's market rates win over catalog.json",
    "B-5": "Components stored as 1P/3P in a category a 1P-HYB/3P-HYB slot draws from (never matched; legacy matching kept)",
    "B-12": "EMI banks skipped for a malformed slug or logo colour",
}
B5_CODE = "hybrid_slot_phase_unmatched"


def classify(label: str, violation: dict) -> str | None:
    """The business default a violation (reported by the importer ``label``) belongs to, if any."""
    code = str(violation.get("code") or violation.get("field") or "")
    if code == "d2_flarize_wins":
        return "B-2"
    if code == "pack_config_market_rate_wins":
        return "B-3"
    if code == B5_CODE:
        return "B-5"
    if label == "emi_bank" and code in ("slug", "logo_bg"):
        return "B-12"
    return None


def describe(violation: dict) -> str:
    """One report line: the importer's message plus both values of every changed field it names (B-2 needs them)."""
    parts = [str(violation.get("message") or "")]
    for difference in violation.get("differences") or []:
        parts.append(f"{difference.get('field')}: {difference.get('bom')!r} → {difference.get('flarize')!r}")
    before, after = violation.get("bom"), violation.get("flarize")
    if isinstance(before, dict) and isinstance(after, dict):
        parts.extend(f"{key}: {before.get(key)!r} → {after.get(key)!r}" for key in sorted(set(before) | set(after)))
    return "; ".join(part for part in parts if part)


def group(labelled: Iterable[tuple[str, dict]]) -> dict[str, list[dict]]:
    """``{"B-2": [violation, …], …}`` over ``(importer label, violation)`` pairs (only the defaults that apply)."""
    grouped: dict[str, list[dict]] = {}
    for label, violation in labelled:
        key = classify(label, violation)
        if key:
            grouped.setdefault(key, []).append(violation)
    return {key: grouped[key] for key in DEFAULTS if key in grouped}


def hybrid_phase_mismatches() -> dict:
    """B-5: components of a category that a ``filter_phase = HYB`` BOM slot draws from whose phase (``attributes.phase``,
    else the inverter spec's) does not contain ``HYB`` — the legacy website quote never selects them for that slot."""
    from bom.models import Slot
    from catalog.models import Component

    slots = Slot.objects.filter(filter_phase="HYB", category__deleted_at__isnull=True, template__deleted_at__isnull=True).select_related("category", "template")
    by_category: dict[int, list[str]] = {}
    for slot in slots:
        by_category.setdefault(slot.category_id, []).append(f"{slot.template.system_type} slot {slot.sort_order}")
    violations = []
    components = Component.objects.filter(category_id__in=list(by_category)).select_related("inverter_spec", "category").order_by("sku")
    for component in components:
        spec = getattr(component, "inverter_spec", None)
        phase = (component.attributes or {}).get("phase") or (spec.phase if spec is not None else None) or ""
        if phase and "HYB" not in str(phase):
            violations.append(
                {
                    "source_table": "catalog_component",
                    "source_id": component.sku,
                    "code": B5_CODE,
                    "severity": "warning",
                    "message": (
                        f"{component.sku} ({component.name}, category {component.category.slug}) has phase {phase!r}: "
                        f"never matched by {', '.join(by_category[component.category_id])} (HYB filter)."
                    ),
                }
            )
    return {"created": 0, "updated": 0, "skipped": len(violations), "violations": violations}
