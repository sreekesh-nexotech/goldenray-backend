"""Inspection snapshots (``site_inspections_snapshot``): what the visit was asked to verify, versioned.

The shape follows the V2 ``buildInspectionSnapshot`` allow-list (spec §E.4) in snake case and **without any price**:
``origin``, ``system_type``, ``agreement`` (uid/number/version, or null), ``quotation_version_uid``, ``customer``,
``site``, ``system`` (type, capacity, phase, tier, panel/inverter/battery component uids, structure) and ``kseb``
(consumer number, registered phone, wheeling). A pre-sale snapshot has the same shape with an empty ``system``.
"""

from __future__ import annotations

from core.services import stamp_create
from site_inspections.models import Inspection, Snapshot
from site_inspections.models.choices import SnapshotSource
from site_inspections.services import common


def _uid(obj) -> str | None:
    return str(obj.uid) if obj is not None else None


def build(inspection: Inspection, *, agreement: dict | None = None) -> dict:
    """The snapshot of ``inspection`` as it stands (after the caller applied an agreement's values)."""
    customer = inspection.customer
    return {
        "origin": inspection.origin,
        "system_type": inspection.system_type,
        "agreement": (
            None if inspection.agreement_uid is None else {"uid": str(inspection.agreement_uid), "number": inspection.agreement_number, "version": inspection.agreement_version, **(agreement or {})}
        ),
        "quotation_version_uid": str(inspection.quotation_version_uid) if inspection.quotation_version_uid else None,
        "customer": {
            "uid": str(customer.uid),
            "code": customer.code,
            "name": customer.name,
            "phone": customer.phone_e164,
            "email": customer.email,
            "address": customer.address,
            "pincode": customer.pincode,
            "district": customer.district,
        },
        "site": {"address": inspection.address, "pincode": inspection.pincode, "location": inspection.location, "district": inspection.district},
        "system": {
            "type": inspection.system_type,
            "capacity_kw": str(inspection.quoted_size_kw) if inspection.quoted_size_kw is not None else None,
            "phase": inspection.phase or None,
            "tier": (agreement or {}).get("tier"),
            "panel": {"uid": _uid(inspection.quoted_panel), "capacity_w": inspection.quoted_panel_capacity_w},
            "inverter": {"uid": _uid(inspection.quoted_inverter)},
            "battery": {"uid": _uid(inspection.quoted_battery)} if inspection.system_type == "HYBRID" else None,
            "structure": {"type": inspection.quoted_structure_type or None, "material": inspection.quoted_structure_material or None},
        },
        "kseb": {
            "consumer_number": inspection.consumer_number or None,
            "registered_phone": inspection.registered_phone_e164 or None,
            "wheeling_required": inspection.wheeling_required,
        },
    }


def add(inspection: Inspection, *, user, source: str, data: dict) -> Snapshot:
    number = common.next_number(Snapshot.all_objects.filter(inspection=inspection))
    snapshot = Snapshot(inspection=inspection, number=number, source=source, agreement_version=inspection.agreement_version if source == SnapshotSource.AGREEMENT else None, data=data)
    stamp_create(snapshot, user)
    snapshot.save()
    return snapshot


def current(inspection: Inspection) -> Snapshot | None:
    return Snapshot.objects.filter(inspection=inspection).order_by("-number").first()
