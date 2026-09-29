"""Site inspections on the customer timeline (``customers/<uid>/timeline/``): created, customer answers, releases.

Registered for the ``site_inspections`` module, so only users who may view inspections get these entries, and only
for inspections inside their record scope.
"""

from __future__ import annotations

from customers.services import timeline
from site_inspections.models import LocationApproval
from site_inspections.models.choices import ApprovalStatus
from site_inspections.services import common


@timeline.register("site_inspections", module=common.MODULE)
def inspection_entries(customer, *, user, before, limit):
    inspections = common.visible(user).filter(customer=customer)
    created = inspections.order_by("-created_at")
    if before is not None:
        created = created.filter(created_at__lt=before)
    for inspection in created[:limit]:
        yield timeline.Entry(
            inspection.created_at,
            "site_inspections.created",
            f"Site inspection {inspection.number} created",
            common.OBJECT_TYPE,
            inspection.uid,
            {"origin": inspection.origin, "status": inspection.status},
        )
    released = inspections.filter(released_at__isnull=False).order_by("-released_at")
    if before is not None:
        released = released.filter(released_at__lt=before)
    for inspection in released[:limit]:
        yield timeline.Entry(inspection.released_at, "site_inspections.released", f"Site inspection {inspection.number} released for installation", common.OBJECT_TYPE, inspection.uid, {})
    answers = LocationApproval.objects.filter(inspection__in=inspections, responded_at__isnull=False, status__in=[ApprovalStatus.APPROVED, ApprovalStatus.REJECTED, ApprovalStatus.SUPERSEDED])
    if before is not None:
        answers = answers.filter(responded_at__lt=before)
    for approval in answers.select_related("inspection").order_by("-responded_at")[:limit]:
        yield timeline.Entry(
            approval.responded_at,
            "site_inspections.location_answered",
            f"Customer answered the installation-location approval of {approval.inspection.number}",
            common.OBJECT_TYPE,
            approval.inspection.uid,
            {"approval": approval.number, "paper": approval.approved_by_staff_id is not None},
        )
