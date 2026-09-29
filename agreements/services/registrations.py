"""What agreements registers with the platform's registries (called from ``AgreementsConfig.ready``).

* ``core.scopes`` — the ``owned`` filter (:mod:`agreements.services.scoping`, imported for its side effect);
* ``customers.services.merge`` — ``agreements_agreement.customer`` follows a merge (and blocks deleting a customer);
* ``customers.services.timeline`` — the customer timeline's agreement entries (issued, accepted, cancelled);
* ``documents.access`` — an agreement PDF is visible to whoever can see the agreement (record scope);
* ``media.usage`` / ``media.folders`` — acceptance scans are referenced and live in a reserved private folder;
* ``catalog.services.usage`` — components named by issued agreements;
* ``core.dashboard`` — counters for the ``agreements`` module.
"""

from __future__ import annotations

from agreements.models import Agreement, AgreementStatus
from agreements.services import scoping
from agreements.services.common import ACCEPTANCE_FOLDER, MODULE, OBJECT_TYPE
from customers.services import timeline

ISSUED_STATES = (AgreementStatus.ISSUED, AgreementStatus.ACCEPTED, AgreementStatus.SUPERSEDED)


def _older(queryset, column: str, before):
    return queryset.filter(**{f"{column}__lt": before}) if before is not None else queryset


@timeline.register("agreements.agreements", module=MODULE)
def agreement_entries(customer, *, user, before, limit):
    agreements = scoping.visible(Agreement.objects.filter(customer=customer), user)
    issued = _older(agreements.filter(issued_at__isnull=False), "issued_at", before).order_by("-issued_at", "-id")[:limit]
    for agreement in issued:
        yield timeline.Entry(
            agreement.issued_at, "agreements.issued", f"{agreement.get_kind_display()} {agreement.number} issued", OBJECT_TYPE, agreement.uid, {"kind": agreement.kind, "revision": agreement.revision}
        )
    accepted = _older(agreements.filter(accepted_at__isnull=False), "accepted_at", before).order_by("-accepted_at", "-id")[:limit]
    for agreement in accepted:
        yield timeline.Entry(agreement.accepted_at, "agreements.accepted", f"{agreement.get_kind_display()} {agreement.number} accepted", OBJECT_TYPE, agreement.uid, {"via": agreement.accepted_via})
    cancelled = _older(agreements.filter(cancelled_at__isnull=False), "cancelled_at", before).order_by("-cancelled_at", "-id")[:limit]
    for agreement in cancelled:
        label = agreement.number or "draft"
        yield timeline.Entry(agreement.cancelled_at, "agreements.cancelled", f"{agreement.get_kind_display()} {label} cancelled", OBJECT_TYPE, agreement.uid, {"reason": agreement.cancel_reason})


def document_visible(user, object_uid) -> bool:
    return scoping.visible(Agreement.objects.filter(uid=object_uid), user).exists()


def component_usage(component):
    from django.db.models import Q

    rows = Agreement.objects.filter(Q(panel=component) | Q(inverter=component) | Q(battery=component), status__in=ISSUED_STATES).values_list("uid", "number", "status")
    for uid, number, status in rows.order_by("-issued_at")[:50]:
        yield {"object_type": OBJECT_TYPE, "object_uid": uid, "label": f"Agreement {number}", "status": status}


def agreement_counts(user) -> dict[str, int]:
    queryset = scoping.visible(Agreement.objects.all(), user)
    return {
        "drafts": queryset.filter(status=AgreementStatus.DRAFT).count(),
        "issued": queryset.filter(status=AgreementStatus.ISSUED).count(),
        "accepted": queryset.filter(status=AgreementStatus.ACCEPTED).count(),
    }


def register() -> None:
    from catalog.services import usage as catalog_usage
    from core import dashboard
    from customers.services import merge
    from documents import access
    from media import folders
    from media import usage as media_usage

    merge.register_dependant(Agreement, "customer", blocks_delete=True)
    access.register(OBJECT_TYPE, module=MODULE, action="view", visible=document_visible)
    media_usage.register(Agreement, "acceptance_asset")
    folders.reserve(ACCEPTANCE_FOLDER)
    catalog_usage.register("agreements.issued")(component_usage)
    dashboard.register(MODULE)(agreement_counts)
