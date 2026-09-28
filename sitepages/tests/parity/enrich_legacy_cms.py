"""Deterministic enrichment of a PRIVATE copy of the legacy CMS database (content-pages parity, phase 1).

Runs inside the legacy CMS, never inside this project, and never against ``legacy_blog_cms`` (the shared, read-only
UAT database)::

    createdb flarize_wp_content_pages_legacy_cms
    pg_restore -d flarize_wp_content_pages_legacy_cms /home/user/platform-reference/uat/legacy_blog_cms.dump
    source /home/user/legacy-env.sh
    cd /home/user/goldenray/goldenray-backend/backend/cms
    DB_NAME=flarize_wp_content_pages_legacy_cms /home/user/.venvs/legacy-cms/bin/python manage.py shell < enrich_legacy_cms.py

The seeded UAT data has no SEO rows, no slot values, no media, no categories, no sections and no unpublished FAQs, so
parity against it alone would never exercise those paths. This script adds exactly those cases through the legacy
models (auto_now timestamps are left to the legacy code). Idempotent: it refuses to run twice.
"""

import os

from django.db import connection
from siteconfig.models import SiteSettings

from accounts.models import AdminUser
from faqs.models import Faq, FaqCategory
from media.models import MediaAsset
from sitepages.models import Page, PageImageSlot, PageSeo, PageTextSlot

assert connection.settings_dict["NAME"] != "legacy_blog_cms", "never write to the shared UAT database"
assert os.environ.get("DB_NAME") == connection.settings_dict["NAME"]
if FaqCategory.objects.filter(slug="subsidy").exists():
    raise SystemExit("already enriched")


def page(route):
    return Page.objects.get(route=route)


settings_row = SiteSettings.load()
settings_row.company_name = "Flarize"
settings_row.save()

editor = AdminUser.objects.create(username="uat-editor", email="", role="editor")

assets = {}
for key, name, alt, width, height in [
    ("hero", "career-hero.webp", "Flarize installers on a rooftop", 1920, 1080),
    ("og", "career-og.webp", "Careers at Flarize", 1200, 630),
    ("home", "home-hero.webp", "", 1600, 900),
]:
    asset = MediaAsset.objects.create(file=f"blog/{name}", cdn_url=f"https://golden-ray.b-cdn.net/cms/general/{name}", mime="image/webp", size=120_000)
    MediaAsset.objects.filter(pk=asset.pk).update(width=width, height=height, alternative_text=alt)
    assets[key] = MediaAsset.objects.get(pk=asset.pk)

# ── Pages ────────────────────────────────────────────────────────────────────────────────────────────────────────────
career = page("/career")
hero = career.image_slots.get(key="hero_background")
hero.asset = assets["hero"]
hero.alt_text = "Our team at work"
hero.updated_by = editor
hero.save()
title = career.text_slots.get(key="hero_title")
title.value = "Build Kerala's solar future with us"
title.updated_by = editor
title.save()
# hero_subtitle stays empty: an empty value is omitted from the public ``text`` map.
PageSeo.objects.create(
    page=career,
    seo_title="Careers at Flarize — solar jobs in Kerala",
    meta_description="Open roles in installation, sales and engineering at Flarize, Kerala's rooftop solar company.",
    canonical_url="https://flarize.com/career",
    og_image=assets["og"],
    schema_type="WebPage",
    schema_extra={"inLanguage": "en-IN", "@type": "Ignored", "name": "must not override"},
    noindex=False,
    updated_by=editor,
)

home = page("/")
PageImageSlot.objects.create(page=home, key="hero_image", label="Hero image", guidance="Landscape", asset=assets["home"], order=0)
PageImageSlot.objects.create(page=home, key="promo_banner", label="Promo banner", guidance="", order=1)  # unset → null
PageTextSlot.objects.create(page=home, key="cta_phone", label="Call-to-action phone", kind="phone", value="+91 62829 22988", max_length=20, order=0)
PageTextSlot.objects.create(page=home, key="cta_email", label="Call-to-action e-mail", kind="email", value="hello@flarize.com", order=1)
PageTextSlot.objects.create(page=home, key="offer_url", label="Offer link", kind="url", value="https://flarize.com/subsidy", order=2)
PageTextSlot.objects.create(page=home, key="hero_note", label="Hero note", kind="long_text", value="Subsidy up to ₹78,000 — we file it for you.", max_length=120, order=3)

PageSeo.objects.create(page=page("/subsidy"), seo_title="PM Surya Ghar subsidy", meta_description="", schema_type="none", noindex=True)
PageSeo.objects.create(page=page("/about"), seo_title="", meta_description="About Flarize.", schema_type="WebPage", og_image=assets["home"])
PageSeo.objects.create(page=page("/privacy"), seo_title="Privacy", schema_type="FAQPage")  # non-WebPage schema → null on the page

Page.objects.filter(route="/resources").update(status="draft")
Page.objects.filter(route="/blog").update(status="archived")
Page.objects.filter(route="/terms").update(description="Terms of service", group="Legal", is_protected=False)

# ── FAQs ─────────────────────────────────────────────────────────────────────────────────────────────────────────────
subsidy_cat = FaqCategory.objects.create(name="Subsidy", slug="subsidy", description="PM Surya Ghar and state schemes", sort_order=1)
finance_cat = FaqCategory.objects.create(name="Financing", slug="financing", is_active=False, sort_order=2)
FaqCategory.objects.create(name="Unused", slug="unused", sort_order=3)

subsidy = page("/subsidy")
for faq in Faq.objects.filter(page=subsidy).order_by("display_order")[:3]:
    faq.category = subsidy_cat
    faq.created_by = editor
    faq.save()
Faq.objects.create(page=subsidy, question="Can I apply for the subsidy twice?", answer="No. <strong>One</strong> subsidy per consumer number.", display_order=7, status="draft")
Faq.objects.create(page=subsidy, question="Incomplete draft", answer="", display_order=8, status="draft")

comparison = page("/solar-comparison")
rows = list(Faq.objects.filter(page=comparison).order_by("display_order"))
for index, faq in enumerate(rows):
    faq.section = "panels" if index < 3 else "inverters"
    faq.display_order = index if index < 3 else index - 3
    faq.save()
# A tie on display_order inside one section: the legacy order falls back to the id.
Faq.objects.filter(pk=rows[4].pk).update(display_order=0)

emi = page("/emi-calculator")
archived = Faq.objects.filter(page=emi).order_by("display_order").last()
archived.status = "archived"
archived.save()
Faq.objects.filter(page=emi, display_order=0).update(category=finance_cat, answer="<p>Yes — <em>0%</em> processing fee on partner banks.</p><ul><li>SBI</li><li>Federal Bank</li></ul>")

hub = page("/faq")
Faq.objects.create(page=hub, question="What does Flarize do?", answer="We design and install rooftop solar in Kerala.", display_order=0, status="published", category=subsidy_cat)
Faq.objects.create(page=hub, question="Where do you work?", answer="Across Kerala.", section="coverage", display_order=0, status="published")
Faq.objects.create(page=hub, question="Blank question answer pair", answer="   ", display_order=1, status="published")  # dropped from FAQPage schema

resources = page("/resources")  # a draft page: its published FAQs are still served (the route itself always renders)
Faq.objects.create(page=resources, question="Where are the guides?", answer="In the resources section.", display_order=0, status="published")

Faq.objects.filter(page=page("/residential"), display_order=1).update(seo_title="Residential FAQ", meta_description="Short.", noindex=True, schema_extra={"k": "v"})
Faq.objects.filter(status="published", published_at__isnull=True).update(published_at="2026-09-01T10:00:00Z")
print("enriched:", Page.objects.count(), "pages,", Faq.objects.count(), "faqs,", MediaAsset.objects.count(), "assets")
