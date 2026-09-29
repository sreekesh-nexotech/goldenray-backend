"""Website revalidation of the other website apps' pages (wired at the wave-1 integration).

Maintained pages and FAQs (content-pages) and job positions (careers-reference) emit their own lifecycle events; the
blog-owned handler turns their site paths into revalidation requests. A published price release (pricing, wired at
the wave-2a integration) revalidates the fixed pages PLAN §3.5 lists. Driven through the real services.
"""

from decimal import Decimal

import pytest

from blog import events
from careers.services import positions
from careers.tests.factories import JobPositionFactory
from catalog.tests.factories import CategoryFactory, ComponentFactory
from company.tests.factories import CompanyProfileFactory
from faqs.services import faqs
from faqs.tests.factories import FaqFactory, PublishedFaqFactory
from pricing.models import PriceKind
from pricing.services import releases
from pricing.tests.factories import MarketRateFactory, MarketRateSetFactory, PriceFactory, gst_config
from sitepages.models import Page
from sitepages.services import pages
from sitepages.tests.factories import PageFactory

pytestmark = pytest.mark.django_db


@pytest.fixture
def sent(fake_revalidation, drain_outbox):
    CompanyProfileFactory(blog_revalidate_url="https://flarize.com/api/revalidate", blog_revalidate_secret="shared-secret")

    def _sent():
        drain_outbox()
        paths = [request["body"]["path"] for request in fake_revalidation.sent]
        fake_revalidation.sent.clear()
        return paths

    return _sent


@pytest.fixture
def user(make_user):
    return make_user()


def test_publishing_and_unpublishing_a_page(sent, user):
    page = PageFactory(route="/about", slug="about", status=Page.Status.DRAFT)
    pages.publish(page, user=user)
    assert sent() == ["/about"]
    pages.unpublish(page, user=user)
    assert sent() == ["/about"]


def test_faq_changes_revalidate_the_pages_they_appear_on(sent, user):
    faq = FaqFactory(page=PageFactory(route="/faq", slug="faq"))
    faqs.publish(faq, user=user)
    assert sent() == ["/faq"]
    moved = PublishedFaqFactory(page=PageFactory(route="/subsidy", slug="subsidy"))
    faqs.update_faq(moved, user=user, data={"page": PageFactory(route="/emi-calculator", slug="emi")})
    assert sorted(sent()) == ["/emi-calculator", "/subsidy"]


def test_job_positions_revalidate_their_page_and_a_moved_one(sent, user):
    position = JobPositionFactory(slug="solar-engineer")
    positions.transition(position, "publish", user=user)
    assert sent() == ["/career/solar-engineer"]
    positions.update_position(position, user=user, data={"slug": "senior-solar-engineer"})
    assert sent() == ["/career/senior-solar-engineer", "/career/solar-engineer"]


def test_a_published_price_release_revalidates_the_priced_pages(sent, user):
    MarketRateFactory(set=MarketRateSetFactory(status="ACTIVE"))
    gst_config()
    component = ComponentFactory(sku="p2", category=CategoryFactory(gst_rate=Decimal("0.0500")))
    PriceFactory(component=component, kind=PriceKind.LIST, amount=Decimal("13585.00"))
    releases.publish(user=user, note="first")
    assert sent() == ["/solar-comparison", "/emi-calculator"]


def test_event_paths():
    assert events.event_paths({"paths": ["/a", "/b"]}) == ["/a", "/b"]
    assert events.event_paths({"path": "/career/x", "previous_path": "/career/y"}) == ["/career/x", "/career/y"]
    assert events.event_paths({"paths": ["/a"], "path": "/a"}) == ["/a"] and events.event_paths({}) == []
