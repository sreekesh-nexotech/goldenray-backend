"""What procurement registers with other registries (called from ``ProcurementConfig.ready``).

* ``catalog.services.usage`` — ``procurement.draft_batches``: a component on a DRAFT batch cannot be deleted;
* ``core.dashboard`` — counters for the ``procurement`` module.
"""

from __future__ import annotations

from procurement.models import Batch, BatchLine, BatchStatus, Supplier


def draft_batch_usage(component):
    for line in BatchLine.objects.filter(component=component, batch__status=BatchStatus.DRAFT, batch__deleted_at__isnull=True).select_related("batch"):
        yield {"object_type": "procurement.batch", "object_uid": line.batch.uid, "label": line.batch.number, "status": line.batch.status}


def procurement_counts(user) -> dict[str, int]:
    batches = Batch.objects.all()
    return {
        "draft_batches": batches.filter(status=BatchStatus.DRAFT).count(),
        "committed_batches": batches.filter(status=BatchStatus.COMMITTED).count(),
        "active_suppliers": Supplier.objects.filter(is_active=True).count(),
    }


def register() -> None:
    from catalog.services import usage
    from core import dashboard

    usage.register("procurement.draft_batches")(draft_batch_usage)
    dashboard.register("procurement")(procurement_counts)
