"""The maintained-pages registry: the website's real routes and the slots their components read (legacy ``seed_pages``).

``manage.py seed_pages`` applies it through :func:`sync_registry`. It is additive and idempotent: a page is created
only when its route is missing and an existing page is never modified (titles, groups, order and status belong to the
maintainers once the page exists); a slot is created only when its ``(page, key)`` is missing, so an existing slot
keeps its value and image. Nothing is ever deleted — a route that leaves this list stays in the database because
FAQs point at it.

A slot is a contract with one React component: add it here the moment a component reads it from the page payload,
never ahead of time. ``/career`` is the career page (slug ``career``, module ``career_page``).
"""

from __future__ import annotations

import re
from dataclasses import dataclass

from django.db import transaction
from django.utils.text import slugify

from audit.services import record
from core.services import stamp_create
from flarize.cache_utils import bump
from sitepages.models import Page, PageImageSlot, PageTextSlot
from sitepages.services.pages import CACHE_NAMESPACE

Kind = PageTextSlot.Kind

#: route, title, group, protected — in list order (``sort_order`` = position, applied only when a page is created).
PAGES: list[tuple[str, str, str, bool]] = [
    ("/", "Home", "Main", True),
    ("/about", "About", "Main", True),
    ("/how-flarize-works", "How Flarize Works", "Main", True),
    ("/contactus", "Contact", "Main", True),
    ("/faq", "FAQ hub", "Main", True),
    ("/residential", "Residential", "Solutions", True),
    ("/commercial", "Commercial", "Solutions", True),
    ("/industrial", "Industrial", "Solutions", True),
    ("/solutions", "Solutions", "Solutions", True),
    ("/subsidy", "Subsidy", "Programmes", True),
    ("/solar-warranty", "Solar Warranty", "Programmes", True),
    ("/solar-referral-program", "Referral Programme", "Programmes", True),
    ("/service-area", "Service Areas", "Programmes", True),
    ("/emi-calculator", "EMI Calculator", "Tools", True),
    ("/advanced-calculator", "Advanced Calculator", "Tools", True),
    ("/solar-comparison", "Solar Comparison", "Tools", True),
    ("/comparison-table", "Solar Comparison Table", "Tools", True),
    ("/inverter-comparison", "Inverter Comparison", "Tools", True),
    ("/inverter-comparison-table", "Inverter Comparison Table", "Tools", True),
    ("/group-purchase", "Group Purchase", "Tools", True),
    ("/quote-analyser", "Quote Analyser", "Tools", True),
    ("/career", "Careers", "Careers", True),
    ("/projects", "Projects", "Content", True),
    ("/resources", "Resources", "Content", True),
    ("/blog", "Blog", "Content", True),
    ("/privacy", "Privacy Policy", "Legal", True),
    ("/terms", "Terms", "Legal", True),
]


@dataclass(frozen=True)
class ImageSlotSpec:
    key: str
    label: str
    guidance: str = ""


@dataclass(frozen=True)
class TextSlotSpec:
    key: str
    label: str
    guidance: str = ""
    kind: str = Kind.SHORT_TEXT
    max_length: int | None = None


#: route → the slots its component reads.
SLOTS: dict[str, dict[str, list]] = {
    "/career": {
        "images": [ImageSlotSpec("hero_background", "Hero background", "Full-width photo behind the headline; landscape, at least 1920×1080.")],
        "text": [
            TextSlotSpec("hero_title", "Hero headline", "One line; keep it under ~70 characters so it stays on two lines on phones.", Kind.SHORT_TEXT, 90),
            TextSlotSpec("hero_subtitle", "Hero sub-headline", "One or two short sentences under the headline.", Kind.LONG_TEXT, 220),
        ],
    },
}

#: Routes whose slug is not the route's own path (PLAN §7.2 #8: the career page is slug ``career``).
SLUG_OVERRIDES = {"/": "home", "/career-page": "career", "/careers": "career"}


def slug_for_route(route: str) -> str:
    """``/`` → ``home``, ``/solar-warranty`` → ``solar-warranty``, ``/a/b`` → ``a-b``."""
    if route in SLUG_OVERRIDES:
        return SLUG_OVERRIDES[route]
    return slugify(re.sub(r"[/_.~]+", "-", route.strip("/"))) or "home"


@transaction.atomic
def sync_registry(*, user=None) -> dict[str, int]:
    """Create missing pages and slots from :data:`PAGES`/:data:`SLOTS`. Returns ``{pages_created, slots_created}``."""
    pages_created = slots_created = 0
    for position, (route, title, group, protected) in enumerate(PAGES):
        if Page.objects.filter(route=route).exists():
            continue
        page = Page(slug=slug_for_route(route), route=route, title=title, group=group, is_protected=protected, status=Page.Status.PUBLISHED, sort_order=position)
        stamp_create(page, user)
        page.save()
        pages_created += 1
    for route, spec in SLOTS.items():
        page = Page.objects.filter(route=route).first()
        if page is None:
            continue
        for position, image in enumerate(spec.get("images", ())):
            if not PageImageSlot.objects.filter(page=page, key=image.key).exists():
                slot = PageImageSlot(page=page, key=image.key, label=image.label, guidance=image.guidance, sort_order=position)
                stamp_create(slot, user)
                slot.save()
                slots_created += 1
        for position, text in enumerate(spec.get("text", ())):
            if not PageTextSlot.objects.filter(page=page, key=text.key).exists():
                slot = PageTextSlot(page=page, key=text.key, label=text.label, guidance=text.guidance, kind=text.kind, max_length=text.max_length, sort_order=position)
                stamp_create(slot, user)
                slot.save()
                slots_created += 1
    result = {"pages_created": pages_created, "slots_created": slots_created}
    if pages_created or slots_created:
        record("sitepages.registry_synced", object_type="sitepages.page", actor=user, after=result)
        bump(CACHE_NAMESPACE)
    return result
