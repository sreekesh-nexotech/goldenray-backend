"""Outbox handlers owned by blog: website revalidation (PLAN §3.5).

Every blog lifecycle event carries the site paths it affected (``paths``); the handler posts them to the company
profile's ``blog_revalidate_url`` through :mod:`blog.services.revalidation` (fail-soft, never raises, so a website
outage never parks events; a duplicate delivery re-posts idempotent ``revalidatePath`` calls).

``website.revalidate_requested`` (``{"paths": [...]}``) is the same hook for the other website apps (pages, FAQs,
careers — legacy weakness §17 #23): they emit it and never import blog.

The other website apps' own lifecycle events are subscribed too (wired at the wave-1 integration; the handler knows
their event names, never their code): pages and FAQs carry ``paths`` (docs/decisions/content-pages.md, decision 10),
job positions carry ``path`` and, after a slug change, ``previous_path`` (docs/decisions/careers-reference.md).
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

#: Lifecycle events of the other website apps (content-pages: sitepages, faqs; careers-reference: careers).
WEBSITE_CONTENT_EVENTS = (
    "sitepages.page_published",
    "sitepages.page_unpublished",
    "sitepages.page_archived",
    "sitepages.page_updated",
    "faqs.faq_published",
    "faqs.faq_unpublished",
    "faqs.faq_archived",
    "faqs.faq_updated",
    "faqs.faqs_reordered",
    "faqs.category_updated",
    "careers.position_published",
    "careers.position_unpublished",
    "careers.position_closed",
    "careers.position_archived",
    "careers.position_updated",
    "careers.position_deleted",
)


def event_paths(payload: dict) -> list:
    """The site paths an event affected: its ``paths`` list, plus a single ``path`` / ``previous_path`` (careers)."""
    paths = list(payload.get("paths") or [])
    for key in ("path", "previous_path"):
        if payload.get(key) and payload[key] not in paths:
            paths.append(payload[key])
    return paths


def revalidate_website(event: Event) -> None:
    revalidation.revalidate(event_paths(event.payload), reason=event.event_type)


for _event_type in REVALIDATING_EVENTS + WEBSITE_CONTENT_EVENTS:
    handler(_event_type)(revalidate_website)
