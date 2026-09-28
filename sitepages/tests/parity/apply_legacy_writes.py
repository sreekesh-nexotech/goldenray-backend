"""Write-path parity, phase 2: apply a fixed list of Studio operations through the LEGACY code on the private copy.

Run after ``enrich_legacy_cms.py`` and after the phase-1 capture (``capture_legacy.py --dataset enriched``)::

    DB_NAME=flarize_wp_content_pages_legacy_cms /home/user/.venvs/legacy-cms/bin/python manage.py shell < apply_legacy_writes.py
    python capture_legacy.py --base-url http://127.0.0.1:<private port> --db flarize_wp_content_pages_legacy_cms \
        --dataset enriched --golden-only after_writes

``OPERATIONS`` (below) is mirrored literally by ``faqs/tests/test_parity.py::test_faq_write_parity`` and
``sitepages/tests/test_parity.py::test_write_parity_page_operations``, which import the phase-1 export,
applies the same operations through the new staff API (legacy ids resolved through ``core_legacy_map``) and compares
every public payload with the ``*_after_writes`` golden files. Each operation goes through the legacy service or
serializer that the legacy Studio endpoint calls, so validation and side effects are the legacy ones.
"""

import os

from django.db import connection

from faqs.models import Faq
from faqs.services import FaqWorkflowError, archive_faq, publish_faq, reorder_faqs, restore_faq, unpublish_faq
from media.models import MediaAsset
from sitepages.models import Page, PageSeo
from sitepages.serializers import PageImageSlotSerializer, PageSeoSerializer, PageTextSlotSerializer

assert connection.settings_dict["NAME"] != "legacy_blog_cms", "never write to the shared UAT database"
assert os.environ.get("DB_NAME") == connection.settings_dict["NAME"]

OPERATIONS = [
    ("faq_reorder", {"page": "/subsidy", "section": "", "order": [31, 27, 9999, 59, 25]}),
    ("faq_publish", {"faq": 101}),
    ("faq_publish", {"faq": 102, "error": True}),
    ("faq_archive", {"faq": 60}),
    ("faq_unpublish", {"faq": 10}),
    ("faq_restore", {"faq": 80}),
    ("text_slot", {"page": "/career", "key": "hero_subtitle", "value": "Join a team that installs 200 rooftops a month."}),
    ("text_slot", {"page": "/career", "key": "hero_title", "value": "x" * 91, "error": True}),
    ("image_slot", {"page": "/", "key": "promo_banner", "asset": 2, "alt_text": ""}),
    ("image_slot", {"page": "/career", "key": "hero_background", "asset": None}),
    ("seo", {"page": "/subsidy", "meta_description": "Get up to ₹78,000 under PM Surya Ghar — we handle the paperwork.", "schema_type": "WebPage"}),
    ("page_status", {"page": "/resources", "status": "published"}),
    ("page_status", {"page": "/terms", "status": "draft"}),
]


def run(name, args):
    if name == "faq_reorder":
        reorder_faqs(Page.objects.get(route=args["page"]).id, args["section"], args["order"])
    elif name in {"faq_publish", "faq_archive", "faq_unpublish", "faq_restore"}:
        service = {"faq_publish": publish_faq, "faq_archive": archive_faq, "faq_unpublish": unpublish_faq, "faq_restore": restore_faq}[name]
        try:
            service(Faq.objects.get(pk=args["faq"]))
        except FaqWorkflowError as exc:
            assert args.get("error"), exc
            print("  refused as expected:", exc, exc.errors)
            return
        assert not args.get("error"), f"{name} {args} should have been refused"
    elif name == "text_slot":
        slot = Page.objects.get(route=args["page"]).text_slots.get(key=args["key"])
        serializer = PageTextSlotSerializer(slot, data={"value": args["value"]}, partial=True)
        if not serializer.is_valid():
            assert args.get("error"), serializer.errors
            print("  refused as expected:", serializer.errors)
            return
        assert not args.get("error")
        serializer.save()
    elif name == "image_slot":
        slot = Page.objects.get(route=args["page"]).image_slots.get(key=args["key"])
        data = {"asset": args["asset"]}
        if "alt_text" in args:
            data["alt_text"] = args["alt_text"]
        serializer = PageImageSlotSerializer(slot, data=data, partial=True)
        serializer.is_valid(raise_exception=True)
        serializer.save()
    elif name == "seo":
        row, _ = PageSeo.objects.get_or_create(page=Page.objects.get(route=args["page"]))
        serializer = PageSeoSerializer(row, data={k: v for k, v in args.items() if k != "page"}, partial=True)
        serializer.is_valid(raise_exception=True)
        serializer.save()
    elif name == "page_status":
        Page.objects.filter(route=args["page"]).update(status=args["status"])  # the legacy PATCH status (Publish permission)
    else:
        raise ValueError(name)


assert MediaAsset.objects.filter(pk=2).exists()
for operation, arguments in OPERATIONS:
    print(operation, {k: v for k, v in arguments.items() if k != "value"})
    run(operation, arguments)
print("applied", len(OPERATIONS), "operations")
