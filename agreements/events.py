"""Outbox handlers owned by agreements (``@core.outbox.handler("<context>.<event>")``).

``quotations.accepted`` (PLAN §3.5: "agreements (draft PA created)") — a DRAFT Purchase Agreement pinned to the
accepted quotation version, owned by the quotation's owner. Idempotent (at-least-once delivery): a version that already
has a live, not cancelled Purchase Agreement gets nothing new.
"""

from __future__ import annotations

from core.outbox import Event, handler


@handler("quotations.accepted")
def draft_purchase_agreement(event: Event) -> None:
    from agreements.services.agreements import draft_from_accepted_quotation

    draft_from_accepted_quotation(event.payload)
