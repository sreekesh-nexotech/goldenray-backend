#!/usr/bin/env python3
"""Build ``tools/parity/corpus.jsonl`` — the request corpus of the legacy-shim parity run (deterministic).

Every old website URL of the inventory (/home/user/platform-reference/website-api-inventory.md) plus the legacy public
reads ``pincodes/`` and ``tariffs/``, with realistic parameter variants: the UAT data's routes, slugs, pincodes and
filter values, the trailing-slash spellings, the refused write methods, and samples of the committed legacy
corpora (BOM ``calculate`` golden requests, the three solar calculators, EMI calculate/quotation, the recorded form
submissions and job applications). Standard library only.

    python tools/parity/build_corpus.py            # rewrites tools/parity/corpus.jsonl
"""

from __future__ import annotations

import gzip
import json
from pathlib import Path
from urllib.parse import quote, urlencode

HERE = Path(__file__).resolve().parent
ROOT = HERE.parents[1]
CASES: list[dict] = []


def add(case_id: str, group: str, method: str, path: str, *, service: str = "backend", query: str = "", body=None, form=None, files=None, write=False, normalise=()):
    CASES.append(
        {
            "id": case_id,
            "group": group,
            "service": service,
            "method": method,
            "path": path,
            "query": query,
            "body": body if body is None or isinstance(body, str) else json.dumps(body, ensure_ascii=False),
            "form": form,
            "files": files,
            "write": write,
            "normalise": list(normalise),
        }
    )


def q(pairs) -> str:
    return urlencode(pairs, quote_via=quote, safe="*[]$,/")


def sample(items: list, count: int) -> list:
    if len(items) <= count:
        return items
    step = len(items) / count
    return [items[int(index * step)] for index in range(count)]


# ── reference lists ─────────────────────────────────────────────────────────────────────────────────────────────────
REFERENCE = ["device-types", "wattages", "room-sizes", "ev-cars", "ev-scooters", "tariffs", "pincodes"]
for key in REFERENCE:
    add(f"reference/{key}", "reference", "GET", f"/api/{key}/", normalise=["rows_by_id"])
    add(f"reference/{key}/ignored-query", "reference", "GET", f"/api/{key}/", query="page=2&page_size=5", normalise=["rows_by_id"])
    add(f"reference/{key}/no-slash", "reference", "GET", f"/api/{key}")
    for method in ("POST", "PUT", "DELETE"):
        add(f"reference/{key}/write-{method.lower()}", "reference", method, f"/api/{key}/", body={})

# ── products ──────────────────────────────────────────────────────────────────────────────────────────────────────
PANEL_QUERIES = [
    [],
    [("type", "bifacial")],
    [("type", "bifacial,monofacial")],
    [("type", "topcon")],
    [("rating", "excellent")],
    [("rating", "excellent,very-good")],
    [("minEfficiency", "21")],
    [("maxEfficiency", "21.5")],
    [("minEfficiency", "20"), ("maxEfficiency", "22")],
    [("minProductWarranty", "12")],
    [("minPerformanceWarranty", "30")],
    [("brand", "Waaree")],
    [("brand", "Waaree,Adani")],
    [("minKeralaScore", "90")],
    [("ids", "1,2")],
    [("ids", "3")],
    [("sort", "efficiency")],
    [("sort", "efficiency"), ("order", "asc")],
    [("sort", "warranty")],
    [("sort", "topRated"), ("order", "asc")],
    [("sort", "wattage"), ("order", "asc")],
    [("sort", "keralaClimateScore")],
    [("type", "bifacial"), ("sort", "efficiency"), ("order", "desc")],
    [("minEfficiency", "abc")],
    [("ids", "x")],
]
PANEL_SORTS = {"topRated": "kerala_climate_score", "keralaClimateScore": "kerala_climate_score", "efficiency": "efficiency", "price": "price_range", "warranty": "performance_warranty"}
INVERTER_SORTS = {"topRated": "kerala_climate_score", "keralaClimateScore": "kerala_climate_score", "efficiency": "maximum_efficiency", "price": "price_range", "warranty": "warranty_years"}


def sort_column(pairs, mapping) -> str:
    value = dict(pairs).get("sort", "kerala_climate_score")
    return mapping.get(value, value)


for index, pairs in enumerate(PANEL_QUERIES):
    add(f"products/solar-panels/{index:02d}", "products", "GET", "/api/solar-panels/", query=q(pairs), normalise=[f"ties_by_id:{sort_column(pairs, PANEL_SORTS)}"])
INVERTER_QUERIES = [
    [],
    [("type", "hybrid")],
    [("type", "string,microinverter")],
    [("tier", "premium")],
    [("tier", "value,mid-range")],
    [("rating", "excellent")],
    [("minWarranty", "10")],
    [("extendableTo", "15")],
    [("brand", "Enphase")],
    [("minKeralaScore", "85")],
    [("ids", "1,6")],
    [("sort", "efficiency")],
    [("sort", "warranty"), ("order", "asc")],
    [("sort", "rated_output_power"), ("order", "asc")],
    [("sort", "topRated")],
    [("minWarranty", "ten")],
]
for index, pairs in enumerate(INVERTER_QUERIES):
    add(f"products/solar-inverters/{index:02d}", "products", "GET", "/api/solar-inverters/", query=q(pairs), normalise=[f"ties_by_id:{sort_column(pairs, INVERTER_SORTS)}"])
add("products/batteries", "products", "GET", "/api/batteries/", normalise=["rows_by_id"])
for key in ("solar-panels", "solar-inverters", "batteries"):
    add(f"products/{key}/no-slash", "products", "GET", f"/api/{key}")
    add(f"products/{key}/write-post", "products", "POST", f"/api/{key}/", body={})

# ── SEO metadata, installation stats ──────────────────────────────────────────────────────────────────────────────
add("seo/metadata", "seo", "GET", "/api/metadata/", normalise=["rows_by_id"])
add("seo/metadata/no-slash", "seo", "GET", "/api/metadata")
add("seo/metadata/write-post", "seo", "POST", "/api/metadata/", body={"page": "x"})
PINCODES = ["683101", "682001", "680002", "688001", "682002", "682020", "688013", "680003", "688012", "689121", "695003", "688008", "695001", "689122", "695014", "680001"]
for pincode in PINCODES + ["686102", "999999", "abc", " 688008", "68800"]:
    add(f"installations/stats/{pincode.strip() or 'blank'}{'-padded' if pincode.startswith(' ') else ''}", "installations", "GET", "/api/installation-stats/", query=q([("pincode", pincode)]))
add("installations/stats/missing", "installations", "GET", "/api/installation-stats/")
add("installations/stats/empty", "installations", "GET", "/api/installation-stats/", query="pincode=")

# ── calculators and EMI (samples of the committed legacy corpora) ─────────────────────────────────────────────────
for key, path in (("basic", "/api/calculate-solar/"), ("basic_v2", "/api/calculate-solar-new/"), ("advanced", "/api/calculate-solar-advanced/")):
    cases = json.loads((ROOT / "calculators/tests/parity/uat" / f"corpus_{key}.json").read_text())["cases"]
    for index, case in enumerate(sample(cases, 36)):
        add(f"calculators/{key}/{index:02d}", "calculators", "POST", path, body=case["request"])
    add(f"calculators/{key}/get", "calculators", "GET", path)
    add(f"calculators/{key}/no-slash-post", "calculators", "POST", path.rstrip("/"), body="{}")
for key, path in (("calculate", "/api/emi-calculator/"), ("quotation", "/api/emi-calculator/quotation/")):
    cases = json.loads((ROOT / "emi/tests/parity/uat" / f"corpus_{key}.json").read_text())["cases"]
    for index, case in enumerate(sample(cases, 36)):
        add(f"emi/{key}/{index:02d}", "emi", "POST", path, body=case["request"])
add("emi/config", "emi", "GET", "/api/emi-calculator/config/", normalise=["timestamps"])
add("emi/config/no-slash", "emi", "GET", "/api/emi-calculator/config")
add("emi/calculate/get", "emi", "GET", "/api/emi-calculator/")

# ── BOM app ───────────────────────────────────────────────────────────────────────────────────────────────────────
golden = json.loads(gzip.decompress((ROOT / "bom/tests/golden/bom_calculate.json.gz").read_bytes()))["cases"]
picked = sample([case for case in golden if case["status"] == 200], 70) + [case for case in golden if case["status"] == 400]
for index, case in enumerate(picked):
    add(f"bom/calculate/{index:03d}", "bom", "POST", "/bom/api/calculate/", body=case["request"])
add("bom/calculate/not-json-object", "bom", "POST", "/bom/api/calculate/", body="[1, 2]")
add("bom/calculate/get", "bom", "GET", "/bom/api/calculate/")
add("bom/quotation-settings", "bom", "GET", "/bom/api/quotation-settings/", normalise=["timestamps"])
add("bom/quotation-settings/no-slash", "bom", "GET", "/bom/api/quotation-settings")
add("bom/quotation-settings/write-put", "bom", "PUT", "/bom/api/quotation-settings/", body={"offer_title": "x"})

# ── forms (write path: the PRIVATE legacy server) ─────────────────────────────────────────────────────────────────
forms = json.loads((ROOT / "leads/tests/fixtures/legacy_backend/forms.json").read_text())
FORM_PATHS = {
    "lead_collection_home": "/api/lead-collection-home/",
    "affiliate_applications": "/api/affiliate-applications/",
    "warranty_service_requests": "/api/warranty-service-requests/",
    "send_otp": "/api/send-otp/",
    "verify_otp": "/api/verify-otp/",
}
for form, path in FORM_PATHS.items():
    for name, case in forms[form].items():
        if name == "repeat_within_30_days":
            continue  # depends on the private server's history of this number (recorded in leads/tests)
        add(f"forms/{form}/{name}", "forms", "POST", path, body=case["request"], write=True, normalise=["created_row"])
    add(f"forms/{form}/get", "forms", "GET", path, write=True)
applications = json.loads((ROOT / "careers/tests/legacy/applications_cases.json").read_text())["cases"]
SAMPLE_FILES = {
    "pdf": "%PDF-1.4\n1 0 obj << /Type /Catalog >> endobj % resume\ntrailer << /Root 1 0 R >>\n%%EOF\n",
    "txt": "plain text resume",
    "fake_pdf": "this is plain text pretending to be a pdf",
}
for case in applications:
    files = {field: [name, SAMPLE_FILES.get(kind, SAMPLE_FILES["pdf"])] for field, (name, kind) in case.get("files", {}).items() if kind in SAMPLE_FILES}
    if len(files) != len(case.get("files", {})):
        continue  # DOC/DOCX/oversized fixtures are exercised by careers/tests (binary files are not in the corpus)
    add(f"forms/job_applications/{case['name']}", "forms", "POST", "/api/job-applications/", form=case["form"], files=files, write=True, normalise=["created_row"])

# ── CMS delivery (/studio-api/api/…) ─────────────────────────────────────────────────────────────────────────────
ARTICLE_SLUGS = ["pm-surya-ghar-subsidy-guide", "on-grid-vs-hybrid-kerala", "how-net-metering-works", "battery-sizing-guide", "solar-panel-cleaning", "draft-inverter-guide", "archived-old-subsidy"]
ARTICLE_QUERIES = [
    [],
    [("populate", "*")],
    [("populate", "*"), ("sort[0]", "publishedOn:desc")],
    [("populate", "*"), ("sort[0]", "publishedOn:asc")],
    [("sort[0]", "title:asc")],
    [("sort[0]", "sortOrder:asc"), ("sort[1]", "publishedOn:desc")],
    [("pagination[page]", "1"), ("pagination[pageSize]", "2")],
    [("pagination[page]", "2"), ("pagination[pageSize]", "2")],
    [("pagination[page]", "9"), ("pagination[pageSize]", "2")],
    [("pagination[pageSize]", "1000")],
    [("pagination[pageSize]", "0")],
    [("pagination[page]", "abc")],
    [("fields[0]", "slug")],
    [("fields[0]", "slug"), ("fields[1]", "publishedOn")],
    [("fields[0]", "title"), ("pagination[pageSize]", "3")],
    [("filters[isFeatured][$eq]", "true")],
    [("filters[isFeatured][$eq]", "false"), ("populate", "*")],
    [("filters[slug][$ne]", "solar-panel-cleaning")],
    [("filters[slug][$in][0]", "battery-sizing-guide"), ("filters[slug][$in][1]", "how-net-metering-works")],
    [("filters[title][$contains]", "Solar")],
    [("filters[title][$eqi]", "solar panel cleaning")],
    [("filters[bogus][$eq]", "1")],
    [("sort[0]", "bogus")],
    [("filters[slug][$eq]", "net-metering-explained"), ("populate", "*")],
    [("filters[slug][$eq]", "no-such-article")],
    [("filters[isFeatured][$eq]", "maybe")],
]
for slug in ARTICLE_SLUGS:
    ARTICLE_QUERIES.append([("filters[slug][$eq]", slug), ("populate", "*")])
for index, pairs in enumerate(ARTICLE_QUERIES):
    add(f"cms/articles/{index:02d}", "cms", "GET", "/studio-api/api/articles", service="cms", query=q(pairs))
add("cms/articles/slash", "cms", "GET", "/studio-api/api/articles/", service="cms", query="populate=*")
for collection in ("case-studies", "authors", "Articles", "nope", "faqs/", "job-positions/", "page-content/"):
    add(f"cms/collection/{collection.strip('/')}{'-slash' if collection.endswith('/') else ''}", "cms", "GET", f"/studio-api/api/{collection}", service="cms")
add("cms/articles/slug-path", "cms", "GET", "/studio-api/api/articles/solar-panel-cleaning", service="cms")
add("cms/articles/write-post", "cms", "POST", "/studio-api/api/articles", service="cms", body={})

FAQ_ROUTES = ["/", "/comparison-table", "/emi-calculator", "/group-purchase", "/how-flarize-works", "/inverter-comparison", "/inverter-comparison-table", "/quote-analyser"]
FAQ_ROUTES += ["/residential", "/solar-comparison", "/solar-referral-program", "/solar-warranty", "/solutions", "/subsidy", "/about", "/nope"]
for route in FAQ_ROUTES:
    add(f"cms/faqs{route if route != '/' else '/home'}", "cms", "GET", "/studio-api/api/faqs", service="cms", query=q([("page", route)]))
add("cms/faqs/section-empty", "cms", "GET", "/studio-api/api/faqs", service="cms", query=q([("page", "/"), ("section", "")]))
add("cms/faqs/section-other", "cms", "GET", "/studio-api/api/faqs", service="cms", query=q([("page", "/subsidy"), ("section", "eligibility")]))
add("cms/faqs/missing-page", "cms", "GET", "/studio-api/api/faqs", service="cms")
add("cms/faqs/route-param", "cms", "GET", "/studio-api/api/faqs", service="cms", query=q([("route", "/")]))

add("cms/job-positions", "cms", "GET", "/studio-api/api/job-positions", service="cms")
for department in ("engineering", "sales", "operations", "nope"):
    add(f"cms/job-positions/department-{department}", "cms", "GET", "/studio-api/api/job-positions", service="cms", query=q([("department", department)]))
for slug in ("solar-installation-engineer", "field-sales-executive", "crs-executive", "site-supervisor-intern", "design-engineer-draft", "store-keeper-closed", "nope"):
    add(f"cms/job-positions/{slug}", "cms", "GET", f"/studio-api/api/job-positions/{slug}", service="cms")
add("cms/job-positions/slug-slash", "cms", "GET", "/studio-api/api/job-positions/solar-installation-engineer/", service="cms")

PAGE_ROUTES = ["/", "/solutions", "/quote-analyser", "/subsidy", "/how-flarize-works", "/inverter-comparison", "/inverter-comparison-table", "/solar-referral-program", "/residential"]
PAGE_ROUTES += ["/solar-warranty", "/emi-calculator", "/group-purchase", "/solar-comparison", "/comparison-table", "/about", "/contactus", "/faq", "/commercial", "/industrial"]
PAGE_ROUTES += ["/service-area", "/advanced-calculator", "/career", "/projects", "/resources", "/blog", "/privacy", "/terms", "/nope", "/about/"]
for route in PAGE_ROUTES:
    add(f"cms/page-content{route if route != '/' else '/home'}", "cms", "GET", "/studio-api/api/page-content", service="cms", query=q([("route", route)]))
add("cms/page-content/missing-route", "cms", "GET", "/studio-api/api/page-content", service="cms")
add("cms/page-content/page-param", "cms", "GET", "/studio-api/api/page-content", service="cms", query=q([("page", "/")]))


def main() -> None:
    ids = [case["id"] for case in CASES]
    assert len(ids) == len(set(ids)), "duplicate case ids"
    (HERE / "corpus.jsonl").write_text("".join(json.dumps(case, ensure_ascii=False) + "\n" for case in CASES))
    print(f"{len(CASES)} requests")


if __name__ == "__main__":
    main()
