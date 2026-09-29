"""Outbox handlers owned by quotations (``@core.outbox.handler("<context>.<event>")``).

``packs.release_published`` (PLAN §3.5: "quotations (warn open drafts)") — every open DRAFT version priced from an
older PackRelease gets a notice (once per release number); issuing it is refused with ``release_superseded`` until
the draft is refreshed (``PATCH …/versions/<n>/`` with ``refresh_release``). Idempotent (at-least-once delivery).
"""

from __future__ import annotations

import logging

from django.db import transaction

from core.outbox import Event, handler

logger = logging.getLogger("flarize.quotations.events")


@handler("packs.release_published")
def warn_open_drafts(event: Event) -> None:
    from quotations.models import Version, VersionStatus

    number = event.payload.get("number")
    if number is None:
        logger.warning("packs.release_published without number", extra={"event_id": event.id})
        return
    notice = {"code": "PACK_RELEASE_SUPERSEDED", "release": number, "message": f"PackRelease #{number} was published; refresh this draft before issuing it."}
    with transaction.atomic():
        drafts = Version.objects.select_for_update().filter(status=VersionStatus.DRAFT, pack_release__number__lt=number)
        for draft in drafts:
            notices = list(draft.notices or [])
            if any(item.get("code") == notice["code"] and item.get("release") == number for item in notices):
                continue
            draft.versioned_update(None, notices=[*notices, notice])
