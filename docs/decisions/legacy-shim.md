# Legacy shim — the old website contracts under `/legacy/` (PLAN §6, DV-4, DV-5)

Work package **legacy-shim** builds the `legacy` app: for every old website URL of the inventory
(`platform-reference/website-api-inventory.md`) plus the legacy public reads `pincodes/` and `tariffs/`, an adapter
under `/legacy/` that answers the OLD request/response contract (status codes, JSON shape and key order, pagination
style — none —, error bodies, trailing-slash behaviour) by calling the new services; the nginx rewrite map listing
exactly those paths; and the parity harness with its corpus and report. Deviations: DV-130 … DV-134.

## What exists

| Area | Where | Notes |
|---|---|---|
| Base view | `legacy/views/base.py` | `LegacyView`: flag `LEGACY_API_SHIM` checked in `dispatch` (off → the platform's plain 404, like an unrouted path), `versioning_class = None`, anonymous, `schema = None`, JSON only; `legacy_exception_handler` (DRF's own exceptions → DRF's default bodies as the legacy DRF servers answered them; `DomainError` → the view's `legacy_error`; anything else → the platform handler). `AppendSlashView`: the legacy `APPEND_SLASH` 301. |
| Main backend reads | `legacy/views/backend.py`, `legacy/services/{reference,products,website}.py` | device-types, wattages, room-sizes, ev-cars, ev-scooters, tariffs, pincodes, solar-panels, solar-inverters, batteries, metadata, installation-stats |
| Calculators | `legacy/views/backend.py`, `legacy/services/calculators.py` | calculate-solar, calculate-solar-new, calculate-solar-advanced, emi-calculator, emi-calculator/config, emi-calculator/quotation |
| Forms | `legacy/views/forms.py`, `legacy/services/forms.py` | lead-collection-home, send-otp, verify-otp, affiliate-applications, warranty-service-requests, job-applications |
| CMS delivery | `legacy/views/cms.py`, `legacy/services/cms.py` | `<collection>` (articles, and any active collection), faqs, job-positions, job-positions/`<slug>`, page-content |
| BOM app | `legacy/views/bom.py`, `legacy/services/website.py` | calculate (B-1), quotation-settings and quotation-testimonials (GET; testimonials wired by the review after quotations was integrated) |
| Ids | `legacy/services/ids.py` | legacy integer ids ↔ platform rows through `core_legacy_map` (DV-130) |
| B-1 (shared, bom) | `bom/services/website_quote.py` (`INTERNAL_KEYS`, `public_body`), `bom/views/quote.py`, `bom/serializers/quote.py` (`BomQuotePublicSerializer`) | the public quote and the shim omit `cost_breakdown` and `totals` |
| nginx | `deploy/nginx/legacy/groups.conf` | the shim groups list exactly the shimmed paths (with the slash-less spelling) |
| Parity | `tools/parity/{harness.py,build_corpus.py,serve.py,corpus.jsonl,approved.json}`, `docs/migration/parity-report.md` | 574 requests; 0 unapproved differences |
| Tests | `legacy/tests/` | 308 tests: routing/flag/throttle/405/301, every adapter replaying the committed legacy goldens, forms, harness |

## Endpoint map (old URL → shim → platform service)

nginx (`shim` mode) sends `/<old path>` to `/legacy/<old path>`. Throttle scopes are those of the canonical endpoints.

| Old URL | Methods | Platform service | Throttle / cache |
|---|---|---|---|
| `/api/device-types/`, `wattages/`, `room-sizes/`, `ev-cars/`, `ev-scooters/`, `tariffs/` | GET | `reference.services.lists` (active rows) | `public_read`, cached (list namespace) |
| `/api/pincodes/` | GET | `reference_pincode_office` rows (the legacy table was the post offices) | `public_read`, cached (`reference:pincodes`) |
| `/api/solar-panels/`, `solar-inverters/` | GET (`type`, `rating`, `tier`, `minEfficiency`, `maxEfficiency`, `minProductWarranty`, `minPerformanceWarranty`, `minWarranty`, `extendableTo`, `brand`, `minKeralaScore`, `ids`, `sort`, `order`) | `catalog.services.public.products_in` (published products) | `public_read`, cached (catalog/pricing/media) |
| `/api/batteries/` | GET | published battery products + `catalog.services.pricing_hooks` (LIST price) | same |
| `/api/metadata/` | GET | `seo.services.metadata.metadata_queryset` | `public_read`, cached (`seo:metadata`, media) |
| `/api/installation-stats/?pincode=` | GET | `leads.services.installations.installation_stats` | `public_read`, cached |
| `/api/calculate-solar/`, `calculate-solar-new/`, `calculate-solar-advanced/` | POST | `calculators.services.calculate.run` (basic / basic_v2 / advanced) | `public_read`, `no-store` |
| `/api/emi-calculator/`, `…/quotation/` | POST | `emi.services.calculator.calculate` / `quotation` | `public_read`, `no-store` |
| `/api/emi-calculator/config/` | GET | `emi.services.calculator.public_config` | `public_read`, cached (price-source namespaces) |
| `/api/lead-collection-home/` | POST | `leads.services.intake.submit_lead(require_verification=False)` (DV-61) | `public_write` |
| `/api/send-otp/`, `/api/verify-otp/` | POST | `leads.services.otp.send_code` / `verify_code`; Indian mobiles only (`normalise_phone(…, mobile_only=True, regions=INDIA)`, as the canonical OTP form: a valid foreign mobile is refused before the provider); an approved code records the enquiry (`submit_lead`, QUOTE_REQUEST, `/advanced-calculator`) | `otp` per phone (`phone_number`; a number that does not normalise → the client-IP bucket) + `otp_ip` |
| `/api/affiliate-applications/`, `/api/warranty-service-requests/` | POST | `leads.services.intake.submit_affiliate_application` / `submit_warranty_request` | `public_write` |
| `/api/job-applications/` | POST (multipart) | `careers.services.applications.submit_application` | `public_write` |
| `/studio-api/api/<collection>` and `…/<collection>/` | GET | `blog.services.delivery` (`active_collection`, `parse_query`, `query_entries`, `page_body`) | `public_read`, cached (query order kept) |
| `/studio-api/api/faqs?page=&section=` | GET | `faqs.services.delivery.faq_list` | `public_read`, cached |
| `/studio-api/api/job-positions[?department=]`, `…/job-positions/<slug>` | GET | `careers.services.public.public_list` / `public_detail` | `public_read`, cached |
| `/studio-api/api/page-content?route=` | GET | `sitepages.services.delivery.page_content(published_page(route=…))` | `public_read`, cached |
| `/bom/api/calculate/` | POST | `bom.services.website_quote.quote` → `public_body` (B-1) | `public_write`, `no-store` |
| `/bom/api/quotation-settings/` | GET | `company.services.profile.current_profile` + `quotation_offer` | `public_read`, cached (company, media) |
| `/bom/api/quotation-testimonials/` | GET | `quotations.services.content.public_testimonials` (active, `show_on_website`, display order) in the legacy `fields = "__all__"` shape: legacy ids, `photo_src` = uploaded photo → `photo_url` → the legacy stock photo, whole-rupee bills (legacy defaults 3200/200 when unknown) | `public_read`, cached (`quotations_testimonials`, media) |

Not shimmed (on purpose): every write method on those read URLs (405; PLAN §6.2 "write endpoints on reference tables
are not shimmed"), the Studio reads of the form URLs (the legacy GET lists needed a login; Studio uses `/api/v1/`),
detail routes (`/api/solar-panels/<id>/` …) and `/api/customer-installations/`, `/api/solar-installations/`,
`/api/solar-installations-new/` (not public in the legacy API: 401 / HTTP 500 / Studio data). They are
`backend_other` in the nginx map (old container until C9, then gone).

## Decisions

1. **The shim adds no logic of its own.** Every answer comes from the owning package's service; the shim renames
   fields, puts legacy ids back and shapes errors. Where the old contract needed an old behaviour the canonical
   endpoint deliberately dropped, the shim reproduces it at the edge: the CMS ignored the indexed `filters[f][$in][0]`
   spelling (the shim strips those keys), the legacy lead form required `phone_number` with DRF's wording, the job
   form keeps DRF's HTML-input rules (an empty optional field is its default; a missing checkbox is false).
2. **Ids (DV-130).** A migrated row answers its legacy integer id (`core_legacy_map.source_id`); a row created on the
   platform answers `10,000,000 + <platform id>` (above every legacy id, stable, resolvable back). Ids the website
   posts back (EMI `size_id`/`installation_id`/`id`, job `position_id`) resolve the same way; an unknown EMI id is a uid
   no size has (the engine's own "not found" answer, as in the calculators-emi parity).
3. **Errors keep the legacy bodies**: `{"error": m}` for the calculators, EMI (the canonical message with `size_uid`
   renamed back), installation stats and product parameters; `{"errors": [...]}` for the quote (`QuoteInvalid.legacy_errors`);
   `{"message": "Validation failed", "status": "error", "errors": {…}}` for the forms (legacy field names);
   the bare serializer errors for the OTP views; `{"detail": …}` for the CMS 404s and DRF's own 405/429/parse errors.
   Where the legacy crashed (HTTP 500) the shim answers 400 with the legacy error shape (DV-132).
4. **Trailing slashes**: main-backend and BOM URLs answer the slash-less spelling with a 301 to the slashed old URL
   (relative `Location`, query kept) — the legacy `APPEND_SLASH`; a slash-less POST (legacy DEBUG 500) gets the same
   301. CMS URLs are routed as the CMS routed them: `faqs`, `job-positions`, `job-positions/<slug>`, `page-content`
   without a slash; `<slug>` and `<slug>/` are collections (so `faqs/` is "Unknown collection 'faqs'").
5. **B-1**: the quote omits `cost_breakdown` and `totals` everywhere public (`POST /api/public/v1/bom/quote/` and the
   shim) through `website_quote.public_body`; `quote()` still returns the full legacy body (the service-level
   byte-parity test is unchanged; `test_the_public_endpoint_serves_the_same_json_without_internal_costs` proves the two
   keys are absent and every other key and byte equals the golden body). No staff quote endpoint exists yet, so no
   caller receives the full body over HTTP (DV-133).
6. **Not in OpenAPI (DV-131)**: `schema = None` on every legacy view — the contracts are the old ones, frozen, and
   documented here and executable in `tools/parity/corpus.jsonl`.
7. **The legacy app owns no table**, so the LEGACY IMPORT CONTRACT (`<app>/services/legacy_import.py`) does not apply:
   every row the shim serves is imported by its owning package's importer (run by `migrations_tools`).

## Parity evidence

* **Unit tests replay the committed legacy goldens through the shim** (`legacy/tests/`): reference lists
  (`reference/tests/legacy/backend_reference.json`), products (`catalog/tests/fixtures/legacy/response_*.json`),
  metadata (`seo/tests/fixtures/legacy_backend/metadata_list.json`, byte-identical), installation stats (44 captured
  single-value queries), the three calculators (718 UAT cases), EMI calculate/quotation (509 cases, legacy integer ids) and config,
  the BOM quote (every 25th captured 200 + all captured 400s), blog delivery (54 captured queries, byte-identical),
  FAQs and page content (both datasets, legacy ids), job positions (28 responses), the recorded form submissions
  (`leads/tests/fixtures/legacy_backend/forms.json`) and 30 job-application cases.
* **Live harness** (`tools/parity/harness.py`): 574 requests built by `tools/parity/build_corpus.py` from the website
  inventory (UAT routes, slugs, pincodes and filter values, trailing-slash spellings, refused methods, 70 BOM golden
  requests + every captured BOM 400, 36 requests per calculator and per EMI computation, the recorded forms and job
  applications) replayed against the legacy servers and the shim. **439 identical, 135 approved, 0 unapproved** —
  `docs/migration/parity-report.md`. Normalisations allowed: heap order of unordered legacy lists (`rows_by_id`),
  tie order of an ORDER BY without tie-breaker (`ties_by_id:<column>`), the id/timestamps of a row created by the
  request (`created_row`), import timestamps of the two settings rows (`timestamps`). Approved differences are narrow
  (status pair + masked keys) and listed with reasons in `tools/parity/approved.json`.

### Parity environment (how the report was produced)

| Side | Setup |
|---|---|
| Legacy reads | shared UAT servers `127.0.0.1:18012` (main backend) and `127.0.0.1:18009` (CMS), GET and pure-computation POSTs only |
| Legacy writes | a private restore of `legacy_goldenapp.dump` (`legacy_shim_backend_priv`) served on `127.0.0.1:18151` with `/home/user/legacy-env.sh`; restored fresh before each run (the legacy OTP view blocks a number for 30 days) |
| New | private restores of both dumps imported with `import_cms` / `import_backend` (wp/migration-website) into `flarize_wp_legacy_shim_parity`; served by `tools/parity/serve.py` (this checkout): shim flag on, throttles lifted, the imported market-rate set activated and the imported LIST prices standing in for PriceRelease #1 (as `verify_migration --list-prices-as-release`; rehearsal-website finding 1) |

```
python tools/parity/build_corpus.py
DB_NAME=flarize_wp_legacy_shim_parity python tools/parity/serve.py 127.0.0.1:18150 &
python tools/parity/harness.py --legacy-backend-writes http://127.0.0.1:18151 --new http://127.0.0.1:18150 \
    --report docs/migration/parity-report.md
```

## Review fixes (adversarial review)

* **Toll fraud**: the shim's OTP views normalised with `mobile_only=True` but without `regions`, so a valid foreign
  mobile (`+14155550100`, `+447911123456`) was texted — the canonical form restricts to `{"IN"}`. Fixed; test
  `test_otp_never_texts_a_valid_foreign_mobile`.
* **OTP phone throttle failed open**: the legacy `phone_number` wrapper handed the canonical throttle a bare object; for
  a number it could not normalise the throttle's client-IP fallback raised `AttributeError`, which the throttle's
  cache-outage guard swallowed (allowed, logged "throttle cache unavailable"). The wrapper now delegates to the request;
  test `test_otp_phone_throttle_falls_back_to_the_client_ip_instead_of_failing_open`.
* **`/bom/api/quotation-testimonials/` wired** once quotations was integrated (`legacy/tests/test_testimonials.py`,
  nginx `bom_public`; live parity: the legacy list byte for byte).

## Wired at integration (wave 4b)

| Old URL | Target service | Status |
|---|---|---|
| `POST /api/verify-otp/` — the legacy `SentQuote` row (`quote_id`, "We already have your details!") | quotations sent-quote record | **Repeat message wired**: a number with an imported `sent_quotes` row (`quotations_email_log` LEGACY_LINK, `to` = `+91…` or 10 digits) or an earlier `/advanced-calculator` quote enquiry answers `{status: approved, message: "We already have your details! Our team will contact you soon."}` without `quote_id`; the enquiry is still recorded as a lead (legacy `record_lead` ran first). **Still open**: quotations has no write service for a website quote request, so a first request answers `quote_id = QUOTE_<8 hex of the lead uid>` and no `quotations_email_log` row is written (the website reads only `status`/`message`) (`legacy/tests/test_forms.py`) |
| `bom_quotationtestimonial` rows | `migrations_tools import_backend` → `quotations.services.legacy_import.import_backend_testimonials` | **Done** at integration 9e3f7d1: `import_backend` step `backend.quotations` imports them (migration-website DV-120) |

## Hand-over notes

* **Cutover (PLAN §6.4)**: flip a group in `switch.conf` to `shim` only with `LEGACY_API_SHIM` on
  (`PATCH /api/v1/settings/flags/`); C9 turns the flag off and deletes this package (nothing imports it —
  import-linter contract).
* **Rebinding (C7)** makes the shim idle: the canonical endpoints are listed per URL above.
* **Re-running parity** needs a fresh private legacy database for the write path (`reset` then restart the private
  server); never point `--legacy-backend-writes` at the shared servers (the harness refuses 18012/18009).
