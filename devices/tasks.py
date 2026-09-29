"""Celery tasks owned by devices (enqueued only inside ``transaction.on_commit``; routed to the ``ingest`` queue)."""

import logging

from celery import shared_task

logger = logging.getLogger("flarize.devices")


@shared_task(name="devices.tasks.purge_adms_evidence", ignore_result=True)
def purge_adms_evidence(months: int = 3) -> dict:
    """Beat, daily: upcoming ``devices_adms_request`` partitions (when connected as the owner) and the retention purge
    (``DEVICES_ADMS_RETENTION_DAYS``, 30): whole partitions out of the window are dropped, older rows deleted."""
    from devices.services import adms_evidence

    result = adms_evidence.maintain(months)
    if not result["owner"]:
        logger.warning("ADMS evidence partitions not maintained: the worker is not the table owner (run maintain_adms_evidence as the owner)")
    logger.info("ADMS evidence purged", extra={"deleted": result["deleted"], "dropped": result["dropped"], "created": result["created"]})
    return {"owner": result["owner"], "created": result["created"], "dropped": result["dropped"], "deleted": result["deleted"]}
