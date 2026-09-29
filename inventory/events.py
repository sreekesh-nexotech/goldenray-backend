"""Outbox handlers owned by inventory (``@core.outbox.handler("<context>.<event>")``).

* ``procurement.batch_committed`` → book the batch's lines into the receiving location
  (:mod:`inventory.services.receiving`; off unless ``INVENTORY_STOCK`` is on and ``INVENTORY_RECEIVING_LOCATION`` is
  set). Idempotent: the outbox delivers at least once.
"""

from core.outbox import Event, handler
from inventory.services import receiving


@handler("procurement.batch_committed")
def book_committed_batch(event: Event) -> None:
    receiving.receive_batch(dict(event.payload), received_at=event.created_at)
