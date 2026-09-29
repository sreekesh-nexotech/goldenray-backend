#!/usr/bin/env python3
"""Capture the legacy CMS delivery responses the new ``content/<collection>/`` endpoint must reproduce byte for byte.

Run against the seeded, read-only legacy CMS (``/home/user/platform-reference/uat/README.md``)::

    python blog/tests/fixtures/capture_golden.py --base http://127.0.0.1:18009

Each variant is a GET (the legacy server is never written to). The raw response body is stored unmodified in
``legacy_cms/golden/<name>.json`` and ``legacy_cms/golden/manifest.json`` records the path, query and status.
``blog/tests/test_delivery_parity.py`` imports ``legacy_cms/tables.json`` through ``blog.services.legacy_import``
and asserts the new endpoint returns identical bytes for every variant.

The **enriched** set covers what the seed leaves empty (CDN media on covers and images, attribute values, a category
without a slug, NULL author/SEO text). It is captured from a *private* restored copy of the CMS database with
``legacy_cms_enriched/enrich.sql`` applied, served by a private legacy server (never the shared read-only one)::

    python blog/tests/fixtures/capture_golden.py --set enriched --base http://127.0.0.1:18147
"""

from __future__ import annotations

import argparse
import json
import urllib.error
import urllib.request
from pathlib import Path

HERE = Path(__file__).resolve().parent / "legacy_cms"
GOLDEN = HERE / "golden"

SLUGS = [
    "pm-surya-ghar-subsidy-guide",
    "on-grid-vs-hybrid-kerala",
    "how-net-metering-works",
    "battery-sizing-guide",
    "solar-panel-cleaning",
    "draft-inverter-guide",  # draft: empty result, no alias
    "archived-old-subsidy",  # archived: empty result
    "net-metering-explained",  # active alias of how-net-metering-works → meta.redirect
    "no-such-article",
]

# (name, query string) — every query goes to /api/articles on the legacy CMS.
VARIANTS: list[tuple[str, str]] = [
    ("list_populate_all", "populate=*&pagination[pageSize]=100"),
    ("list_default", ""),
    ("fields_slug", "fields[0]=slug"),
    ("fields_slug_title_100", "fields[0]=slug&fields[1]=title&pagination[pageSize]=100"),
    ("fields_dates_and_unknown", "fields[0]=slug&fields[1]=publishedOn&fields[2]=updatedAt&fields[3]=nonexistent"),
    *[(f"slug_{slug.replace('-', '_')}", f"populate=*&filters[slug][$eq]={slug}") for slug in SLUGS],
    ("alias_with_fields", "filters[slug][$eq]=net-metering-explained&fields[0]=slug"),
    ("alias_eqi", "filters[slug][$eqi]=NET-METERING-EXPLAINED"),
    ("filter_eqi", "filters[slug][$eqi]=HOW-NET-METERING-WORKS"),
    ("filter_in_repeated", "filters[slug][$in]=battery-sizing-guide&filters[slug][$in]=solar-panel-cleaning"),
    ("filter_contains", "filters[title][$contains]=solar"),
    ("filter_ne", "filters[slug][$ne]=battery-sizing-guide"),
    ("filter_null_true", "filters[sortOrder][$null]=true"),
    ("filter_null_false", "filters[sortOrder][$null]=false"),
    ("filter_gt", "filters[readTime][$gt]=5"),
    ("filter_gte", "filters[readTime][$gte]=6"),
    ("filter_lt", "filters[readTime][$lt]=6"),
    ("filter_lte", "filters[readTime][$lte]=5"),
    ("filter_published_on_gte", "filters[publishedOn][$gte]=2026-05-03T00:00:00Z"),
    ("filter_is_featured", "filters[isFeatured][$eq]=True"),
    ("filter_document_id", "filters[documentId][$eq]=3c41703e-65e1-4381-b354-b87b4913eb9f"),
    ("filter_unknown_field_ignored", "filters[password][$contains]=x"),
    ("filter_slug_and_title_no_alias", "filters[slug][$eq]=net-metering-explained&filters[title][$contains]=KSEB"),
    ("sort_title_asc", "sort[0]=title:asc"),
    ("sort_title_desc", "sort[0]=title:desc"),
    ("sort_published_on_desc", "sort[0]=publishedOn:desc"),
    ("sort_read_time_asc", "sort[0]=readTime:asc"),
    ("sort_sort_order_asc", "sort[0]=sortOrder:asc"),
    ("sort_sort_order_desc", "sort[0]=sortOrder:desc"),
    ("sort_plain_published_at_desc", "sort=publishedAt:desc"),
    ("sort_two_keys", "sort[0]=isFeatured:desc&sort[1]=title:asc"),
    ("sort_unknown_ignored", "sort[0]=password:desc"),
    ("page_2_size_2", "pagination[page]=2&pagination[pageSize]=2"),
    ("page_3_size_2", "pagination[page]=3&pagination[pageSize]=2"),
    ("page_past_end", "pagination[page]=9&pagination[pageSize]=2"),
    ("page_size_clamped", "pagination[pageSize]=500"),
    ("page_zero_floored", "pagination[page]=0&pagination[pageSize]=3"),
    ("page_size_not_a_number", "pagination[pageSize]=abc"),
    ("sort_and_page", "sort[0]=title:asc&pagination[page]=2&pagination[pageSize]=2&fields[0]=slug"),
    # Order-significant variants: the CMS applies sort keys in query-string order and a repeated filter's last value,
    # so these pairs differ only in parameter order and must never share a cached response.
    ("sort_two_keys_reversed", "sort[1]=title:asc&sort[0]=isFeatured:desc"),
    ("sort_repeated_title_then_read_time", "sort=title:asc&sort=readTime:desc"),
    ("sort_repeated_read_time_then_title", "sort=readTime:desc&sort=title:asc"),
    ("filter_eq_repeated_last_wins", "filters[slug][$eq]=how-net-metering-works&filters[slug][$eq]=battery-sizing-guide&fields[0]=slug"),
    ("filter_eq_repeated_last_wins_reversed", "filters[slug][$eq]=battery-sizing-guide&filters[slug][$eq]=how-net-metering-works&fields[0]=slug"),
    ("page_repeated_last_wins", "pagination[pageSize]=2&pagination[page]=1&pagination[page]=2&fields[0]=slug"),
    ("page_repeated_last_wins_reversed", "pagination[pageSize]=2&pagination[page]=2&pagination[page]=1&fields[0]=slug"),
]


# The enriched private copy: the full list and every published slug (media, attributes, NULL texts, NULL category slug).
ENRICHED_VARIANTS: list[tuple[str, str]] = [
    ("list_populate_all", "populate=*&pagination[pageSize]=100"),
    ("fields_scalars", "fields[0]=slug&fields[1]=readTime&fields[2]=warning&fields[3]=insights"),
    *[(f"slug_{slug.replace('-', '_')}", f"populate=*&filters[slug][$eq]={slug}") for slug in SLUGS[:5]],
    ("sort_read_time", "sort[0]=readTime:asc&fields[0]=slug&fields[1]=readTime"),
    ("filter_read_time_null", "filters[readTime][$null]=true&fields[0]=slug"),
]
SETS = {"seeded": (VARIANTS, GOLDEN), "enriched": (ENRICHED_VARIANTS, Path(__file__).resolve().parent / "legacy_cms_enriched" / "golden")}


def capture(base: str, variant_set: str = "seeded") -> list[dict]:
    variants, out = SETS[variant_set]
    out.mkdir(parents=True, exist_ok=True)
    manifest = []
    for name, query in variants:
        url = f"{base.rstrip('/')}/api/articles" + (f"?{query}" if query else "")
        request = urllib.request.Request(url, method="GET", headers={"Accept": "application/json"})
        try:
            with urllib.request.urlopen(request, timeout=10) as response:  # noqa: S310 - fixed local legacy URL
                status, body = response.status, response.read()
        except urllib.error.HTTPError as exc:
            status, body = exc.code, exc.read()
        (out / f"{name}.json").write_bytes(body)
        manifest.append({"name": name, "query": query, "status": status})
    (out / "manifest.json").write_text(json.dumps(manifest, indent=1) + "\n", encoding="utf-8")
    return manifest


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--base", default="http://127.0.0.1:18009")
    parser.add_argument("--set", default="seeded", choices=sorted(SETS), dest="variant_set")
    args = parser.parse_args()
    for item in capture(args.base, args.variant_set):
        print(item["status"], item["name"])


if __name__ == "__main__":
    main()
