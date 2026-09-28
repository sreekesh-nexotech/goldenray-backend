"""Outbox handlers owned by blog: website revalidation (PLAN §3.5).

Every blog lifecycle event carries the site paths it affected (``paths``); the handler posts them to the company
profile's ``blog_revalidate_url`` through :mod:`blog.services.revalidation` (fail-soft, never raises, so a website
outage never parks events; a duplicate delivery re-posts idempotent ``revalidatePath`` calls).

``website.revalidate_requested`` (``{"paths": [...]}``) is the same hook for the other website apps (pages, FAQs,
careers — legacy weakness §17 #23): they emit it and never import blog.
"""

from blog.services import revalidation
from core.outbox import Event, handler

REVALIDATING_EVENTS = (
    "blog.entry_published",
    "blog.entry_unpublished",
    "blog.entry_archived",
    "blog.entry_deleted",
    "blog.entry_updated",
    "blog.entry_slug_changed",
    "blog.content_changed",
    "website.revalidate_requested",
)


def revalidate_website(event: Event) -> None:
    revalidation.revalidate(event.payload.get("paths") or [], reason=event.event_type)


for _event_type in REVALIDATING_EVENTS:
    handler(_event_type)(revalidate_website)
