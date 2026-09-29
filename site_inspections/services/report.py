"""Inspection report (``GET site-inspections/<uid>/report/?variant=customer|internal``).

One HTML template per variant rendered by the documents pipeline (``INSPECTION_REPORT`` render job, Playwright);
the payload is frozen here. Sections follow the V2 two-page report (spec §H) with its data defects fixed: equipment
and observations are actually loaded (§J 31), additional work lists only identified items (§J 32), measurements come
from ``width_m``/``height_m`` and rectangles are drawn from image-normalised geometry (§J 33).

* ``customer`` — no internal remarks, no review notes, no prices (the inspection has none);
* ``internal`` — adds remarks, readiness blockers, engineering reviews, per-check equipment results, cable details.

Photos are embedded as data URIs of their 480 px thumbnails (the renderer fetches nothing); a job with the same
payload is reused. Filename as V2: ``{customer} - {kW} kW - {Hybrid|On-Grid} - Site Inspection Report - {date}.pdf``.
"""

from __future__ import annotations

import base64
import re

from django.conf import settings
from django.db import transaction

from company.services.profile import current_profile
from core.errors import DomainError
from documents.services.jobs import request_render
from engines import inspection_checks as checks
from media.services.storage import StorageError, storage_for
from media.services.thumbnails import render_thumbnail
from site_inspections.models import Inspection, LocationApproval, Photo
from site_inspections.models.choices import ApprovalStatus
from site_inspections.services import common, equipment, photos, snapshots, work

VARIANTS = ("customer", "internal")
KIND = "INSPECTION_REPORT"
MAX_EVIDENCE = 8
IMAGE_BUDGET = 700_000  # bytes of embedded images (the documents payload is capped at 1 MB)
DISCLAIMER = (
    "This report records the site conditions, proposed installation locations and customer approval captured during the site inspection. "
    "Final installation remains subject to engineering and safety requirements."
)


def _label(inspection: Inspection, name: str):
    value = getattr(inspection, name)
    if value in (None, ""):
        return "—"
    if isinstance(value, bool):
        return "Yes" if value else "No"
    display = getattr(inspection, f"get_{name}_display", None)
    return display() if display else value


def _rows(inspection: Inspection, pairs) -> list[list]:
    return [[label, _label(inspection, name)] for label, name in pairs]


class _Images:
    def __init__(self):
        self.used = 0

    def data_uri(self, photo: Photo | None) -> str | None:
        if photo is None:
            return None
        asset = photo.asset
        storage = storage_for(asset.visibility)
        try:
            data = storage.read(asset.thumbnail_key) if asset.thumbnail_key else render_thumbnail(storage.read(asset.file), int(settings.MEDIA_THUMBNAIL_MAX_PX))
        except (StorageError, OSError, ValueError):
            return None
        if self.used + len(data) > IMAGE_BUDGET:
            return None
        self.used += len(data)
        return f"data:image/webp;base64,{base64.b64encode(data).decode()}"


def _location(images: _Images, inspection: Inspection, annotation_type: str, title: str) -> dict:
    current = {a.annotation_type: a for a in common.current_annotations(inspection)}.get(annotation_type)
    fallback = inspection.panel_photo if annotation_type == "PANEL_AREA" else inspection.equipment_photo
    photo = current.photo if current is not None else fallback
    geometry = current.geometry if current is not None else None
    return {
        "title": title,
        "image": images.data_uri(photo),
        "box": {key: round(float(geometry[key]) * 100, 3) for key in ("x", "y", "w", "h")} if geometry else None,
        "legacy_geometry": bool(current and current.geometry_space == "LEGACY_CONTAINER"),
        "width_m": current.width_m if current else None,
        "height_m": current.height_m if current else None,
        "area_m2": current.area_m2 if current else None,
    }


def filename(inspection: Inspection) -> str:
    size = f"{inspection.quoted_size_kw.normalize()} kW" if inspection.quoted_size_kw is not None else "— kW"
    kind = "Hybrid" if inspection.system_type == "HYBRID" else "On-Grid"
    name = f"{inspection.customer.name} - {size} - {kind} - Site Inspection Report - {inspection.visit_date.isoformat()}.pdf"
    return re.sub(r'[<>:"/\\|?*]', "-", name)


def payload(inspection: Inspection, variant: str) -> dict:
    internal = variant == "internal"
    images = _Images()
    customer = inspection.customer
    assessments = [a for a in equipment.assessments(inspection) if a.pk]
    layout_ids = {inspection.panel_photo_id, inspection.equipment_photo_id}
    evidence = [p for p in photos.photos_queryset(inspection) if p.pk not in layout_ids][:MAX_EVIDENCE]
    approval = LocationApproval.objects.filter(inspection=inspection, status=ApprovalStatus.APPROVED).order_by("-number").first()
    items = [item for item in work.items_queryset(inspection) if item.required and item.status != "NONE"]
    profile = current_profile()
    body = {
        "variant": variant,
        "company": profile.trade_name or profile.legal_name,
        "number": inspection.number,
        "status": inspection.get_status_display(),
        "visit_date": inspection.visit_date.isoformat(),
        "customer": [
            ["Name", customer.name],
            ["Phone", customer.phone_e164 or "—"],
            ["Email", customer.email or "—"],
            ["Address", inspection.address or customer.address or "—"],
            ["District", inspection.district or "—"],
            ["Pincode", inspection.pincode or "—"],
        ],
        "system": [
            ["System", f"{inspection.quoted_size_kw.normalize()} kW" if inspection.quoted_size_kw is not None else "—"],
            ["System type", inspection.get_system_type_display()],
            ["Phase", _label(inspection, "phase")],
            ["Panel", inspection.quoted_panel.name if inspection.quoted_panel else "—"],
            ["Inverter", inspection.quoted_inverter.name if inspection.quoted_inverter else "—"],
            ["Battery", inspection.quoted_battery.name if inspection.quoted_battery else "None"],
        ],
        "site": _rows(inspection, [("Location", "location"), ("Road access", "road_access"), ("Latitude", "latitude"), ("Longitude", "longitude")]),
        "map_link": inspection.google_map_link,
        "locations": [_location(images, inspection, "PANEL_AREA", "Panel area"), _location(images, inspection, "EQUIPMENT_AREA", "Inverter + ACDB + DCDB area")],
        "roof": _rows(
            inspection,
            [
                ("Building", "building_type"),
                ("Roof type", "roof_type"),
                ("Roof condition", "roof_condition"),
                ("Roof strength", "roof_strength"),
                ("Accessibility", "roof_accessibility"),
                ("Direction", "direction_facing"),
                ("Structure", "site_structure_type"),
            ],
        ),
        "shading": _rows(
            inspection,
            [("Morning", "morning_shading"), ("Afternoon", "afternoon_shading"), ("Shade source", "shade_source"), ("Approx. shading %", "shading_pct"), ("Generation impact", "generation_impact")],
        ),
        "electrical": _rows(
            inspection,
            [
                ("Consumer no.", "consumer_number"),
                ("Phase", "phase"),
                ("Connected load (kW)", "connected_load_kw"),
                ("Neutral link", "neutral_link"),
                ("Termination point", "termination_point"),
                ("Distribution board", "distribution_board"),
                ("Earthing", "earthing"),
            ],
        ),
        "equipment": [
            {
                "title": checks.checklist(a.equipment_type, a.checks_version).title,
                "status": a.get_status_display(),
                "review": a.get_review_status_display(),
                "issue": a.issue,
                "corrective_action": a.corrective_action,
                "checks": [[c.label, (a.results or {}).get(c.id, "NOT_CHECKED")] for c in checks.checklist(a.equipment_type, a.checks_version).checks] if internal else [],
            }
            for a in assessments
        ],
        "evidence": [{"label": p.get_photo_type_display(), "caption": p.caption, "image": images.data_uri(p)} for p in evidence],
        "additional_work": [
            {"label": i.get_work_type_display(), "status": i.get_status_display(), "quantity": f"{i.quantity} {i.unit}".strip() if i.quantity is not None else "", "dimensions": i.dimensions}
            for i in items
        ],
        "suitability": inspection.get_site_suitability_display() if inspection.site_suitability else "—",
        "observations": [{"category": o.get_category_display(), "note": o.note} for o in work.observations_queryset(inspection)] if internal else [],
        "approval": (
            None
            if approval is None
            else {
                "customer": approval.customer_name,
                "number": approval.number,
                "at": approval.responded_at.isoformat() if approval.responded_at else None,
                "via": "Paper" if approval.approved_by_staff_id else "OTP",
                "comment": approval.customer_comment,
            }
        ),
        "disclaimer": DISCLAIMER,
        "filename": filename(inspection),
    }
    if internal:
        state = common.readiness(inspection)
        body["internal"] = {
            "remarks": [
                ["Engineer remarks", inspection.engineer_remarks or "—"],
                ["Approval remarks", inspection.approval_remarks or "—"],
                ["Final recommendation", inspection.final_recommendation or "—"],
                ["Additional requirements", inspection.additional_requirements or "—"],
            ],
            "cables": _rows(
                inspection,
                [
                    ("AC cable (m)", "ac_cable_m"),
                    ("DC cable (m)", "dc_cable_m"),
                    ("LA cable (m)", "la_cable_m"),
                    ("Battery cable (m)", "battery_cable_m"),
                    ("Battery cable route", "battery_cable_route"),
                ],
            ),
            "complexity": inspection.get_complexity_status_display(),
            "blockers": [blocker["text"] for blocker in state["blockers"]],
            "reviews": [{"trigger": r.get_trigger_display(), "decision": r.get_decision_display(), "reason": r.reason, "notes": r.notes} for r in inspection.engineering_reviews.all()],
            "snapshot_number": getattr(snapshots.current(inspection), "number", None),
        }
    return body


@transaction.atomic
def request_report(inspection: Inspection, *, user, variant: str):
    if variant not in VARIANTS:
        raise DomainError("validation_error", "Choose the customer or the internal report.", errors={"variant": ["Use customer or internal."]})
    job = request_render(KIND, common.OBJECT_TYPE, inspection.uid, variant, "en", payload(inspection, variant), user, reuse=True)
    common.audit("report_requested", inspection, user, after={"variant": variant, "job": str(job.uid)})
    return job
