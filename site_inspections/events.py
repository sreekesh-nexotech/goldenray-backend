"""Outbox handlers owned by site_inspections (``@core.outbox.handler("<context>.<event>")``).

``agreements.issued`` / ``agreements.superseded`` → ``services.linking.apply_agreement`` (idempotent on the agreement
uid; payload contract in ``docs/decisions/site-inspections.md``). Emitted by this context: ``site_inspections.released``.
"""

from core.outbox import Event, handler
from site_inspections.services import linking


@handler("agreements.issued")
def link_issued_agreement(event: Event) -> None:
    linking.apply_agreement(event.payload)


@handler("agreements.superseded")
def refresh_superseded_agreement(event: Event) -> None:
    linking.apply_agreement(event.payload)
