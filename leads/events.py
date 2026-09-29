"""Outbox handlers owned by leads (``@core.outbox.handler("<context>.<event>")``).

``quotations.issued`` (PLAN §3.5: consumers include leads — "mark CONVERTED"). Payload contract for the quotations
package: ``{"quotation_uid": "<uuid>", "customer_uid": "<uuid>", ...}``. The customer's open leads (linked, or unlinked from its phone) become CONVERTED
(idempotent: already converted leads are left alone; an unknown or merged-away customer is ignored).
"""

from __future__ import annotations

import logging

from core.outbox import Event, handler

logger = logging.getLogger("flarize.leads.events")


@handler("quotations.issued")
def convert_leads_of_quoted_customer(event: Event) -> None:
    from customers.models import Customer
    from leads.services.leads import mark_converted_for_customer

    customer_uid = event.payload.get("customer_uid")
    if not customer_uid:
        logger.warning("quotations.issued without customer_uid", extra={"event_id": event.id})
        return
    customer = Customer.objects.filter(uid=customer_uid).first()
    if customer is None:
        return
    mark_converted_for_customer(customer, reason=f"quotation issued ({event.payload.get('quotation_uid', '')})")
