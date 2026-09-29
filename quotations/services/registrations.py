"""What quotations registers with the platform's registries (called from ``QuotationsConfig.ready``).

* ``core.scopes`` — the ``owned`` filter (:mod:`quotations.services.scoping`);
* ``customers.services.merge`` — ``quotations_quotation.customer`` follows a merge (and blocks deleting a customer);
* ``customers.services.timeline`` — the customer timeline's quotation entries (created, issued, accepted, cancelled);
* ``documents.access`` — a quotation PDF is visible to whoever can see its quotation (record scope);
* ``media.usage`` — testimonial photos and campaign images cannot be deleted while used;
* ``catalog.services.usage`` — components printed in issued quotations;
* ``core.dashboard`` — counters for the ``quotations`` module.
"""

from __future__ import annotations

from customers.services import timeline
from quotations.models import BomSnapshot, Campaign, Quotation, QuotationStatus, Testimonial, Version, VersionStatus
from quotations.services import scoping
from quotations.services.common import MODULE


def _older(queryset, column: str, before):
    return queryset.filter(**{f"{column}__lt": before}) if before is not None else queryset


@timeline.register("quotations.quotations", module=MODULE)
def quotation_entries(customer, *, user, before, limit):
    quotations = scoping.visible(Quotation.objects.filter(customer=customer), user)
    for quotation in _older(quotations, "created_at", before).order_by("-created_at", "-id")[:limit]:
        yield timeline.Entry(quotation.created_at, "quotations.created", f"Quotation {quotation.number or 'draft'} created", "quotations.quotation", quotation.uid, {"status": quotation.status})
        if quotation.accepted_at and (before is None or quotation.accepted_at < before):
            yield timeline.Entry(quotation.accepted_at, "quotations.accepted", f"Quotation {quotation.number} accepted", "quotations.quotation", quotation.uid, {})
        if quotation.cancelled_at and (before is None or quotation.cancelled_at < before):
            yield timeline.Entry(
                quotation.cancelled_at, "quotations.cancelled", f"Quotation {quotation.number or 'draft'} cancelled", "quotations.quotation", quotation.uid, {"reason": quotation.lost_reason}
            )
    issued = scoping.visible(Version.objects.filter(quotation__customer=customer, issued_at__isnull=False, status__in=[VersionStatus.ISSUED, VersionStatus.SUPERSEDED]), user)
    for version in _older(issued, "issued_at", before).select_related("quotation").order_by("-issued_at", "-id")[:limit]:
        yield timeline.Entry(
            version.issued_at,
            "quotations.issued",
            f"Quotation {version.quotation.number} v{version.number} issued",
            "quotations.quotation",
            version.quotation.uid,
            {"version": version.number, "final_price": str(version.final_price) if version.final_price is not None else None},
        )


def document_visible(user, object_uid) -> bool:
    return scoping.visible(Version.objects.filter(uid=object_uid), user).exists()


def component_usage(component):
    rows = BomSnapshot.objects.filter(lines__contains=[{"componentId": component.sku}], quotation_version__status=VersionStatus.ISSUED).values_list(
        "quotation_version__quotation__uid", "quotation_version__quotation__number"
    )
    for uid, number in rows.distinct()[:50]:
        yield {"object_type": "quotations.quotation", "object_uid": uid, "label": f"Quotation {number}", "status": "ISSUED"}


def quotation_counts(user) -> dict[str, int]:
    queryset = scoping.visible(Quotation.objects.all(), user)
    return {
        "drafts": queryset.filter(status=QuotationStatus.DRAFT).count(),
        "issued": queryset.filter(status=QuotationStatus.ISSUED).count(),
        "accepted": queryset.filter(status=QuotationStatus.ACCEPTED).count(),
    }


def register() -> None:
    from catalog.services import usage as catalog_usage
    from core import dashboard
    from customers.services import merge
    from documents import access
    from media import usage as media_usage

    merge.register_dependant(Quotation, "customer", blocks_delete=True)
    access.register("quotations.version", module=MODULE, action="view", visible=document_visible)
    media_usage.register(Testimonial, "photo")
    media_usage.register(Campaign, "image")
    catalog_usage.register("quotations.issued")(component_usage)
    dashboard.register(MODULE)(quotation_counts)
