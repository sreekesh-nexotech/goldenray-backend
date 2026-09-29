"""System reactions to other contexts' events (see ``packs/events.py``)."""

from __future__ import annotations

from django.db import transaction
from django.utils import timezone

from audit.services import record
from flarize.cache_utils import bump
from packs.models import ConfigVersion
from packs.services import mirror
from packs.services.versions import AUTHORING_NAMESPACE, open_draft


@transaction.atomic
def refresh_open_draft(*, reason: str) -> dict | None:
    draft = open_draft()
    if draft is None:
        return None
    locked = ConfigVersion.objects.select_for_update().get(pk=draft.pk)
    outcome = mirror.mirror(locked, user=None)
    record("packs.config_mirror_refreshed", obj=locked, after={"number": locked.number, "reason": reason, **outcome})
    bump(AUTHORING_NAMESPACE)
    return outcome


@transaction.atomic
def price_release_published(*, number) -> dict | None:
    draft = open_draft()
    if draft is None:
        return None
    locked = ConfigVersion.objects.select_for_update().get(pk=draft.pk)
    marker = f"PriceRelease #{number} published"
    if any(entry.get("event") == "pricing.release_published" and entry.get("note") == marker for entry in locked.change_log or []):
        return None
    entry = {"at": timezone.now().isoformat(), "by": "system", "section": "*", "note": marker, "changed": False, "event": "pricing.release_published", "stale": True}
    locked.versioned_update(None, change_log=[*(locked.change_log or []), entry])
    outcome = mirror.mirror(locked, user=None)
    record("packs.config_marked_stale", obj=locked, after={"number": locked.number, "price_release": number, **outcome})
    bump(AUTHORING_NAMESPACE)
    return outcome
