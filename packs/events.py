"""Outbox handlers owned by packs (``@core.outbox.handler("<context>.<event>")``).

* ``pricing.release_published`` — PLAN §3.5 "packs (mark stale drafts)": the open draft's typed mirror is rebuilt on
  the new prices and its ``change_log`` records that its checker run and release preview predate the release;
* ``catalog.component_status_changed`` / ``catalog.component_deleted`` — a component leaving (or rejoining) the
  selectable set changes what the BOM builder resolves: the open draft's mirror is rebuilt.

Idempotent (at-least-once delivery): rebuilding the mirror twice gives the same rows; the stale marker is recorded
once per release number.
"""

from core.outbox import Event, handler
from packs.services import maintenance


@handler("pricing.release_published")
def mark_drafts_stale(event: Event) -> None:
    maintenance.price_release_published(number=event.payload.get("number"))


@handler("catalog.component_status_changed")
def component_status_changed(event: Event) -> None:
    maintenance.refresh_open_draft(reason=f"component {event.payload.get('sku', '')} is now {event.payload.get('to', '')}")


@handler("catalog.component_deleted")
def component_deleted(event: Event) -> None:
    maintenance.refresh_open_draft(reason="a component was deleted")
