# Website UAT — the GoldenRay website against the platform's `/legacy/` shim

User acceptance test of PLAN §6 (strangler cutover) and §8 ("Public contract … zero unapproved diffs"): the real
website (`/home/user/goldenray/frontend`, unchanged) run twice — once against the two legacy Django servers, once
against the platform — and driven through every public page and flow in a real browser; every rendered page, console
error, failed request and API response compared. Run 2026-09-29/30 on branch `uat/website` (from
`claude/bold-goodall-lxgwep` 11711b8, re-merged at cfc3488 after the final review; W-1 re-verified on the merged code).

## Result

| | Count |
|---|---|
| Rows in the page/flow matrix | 58 (33 pages or page groups, 25 flows) |
| PASS | 38 |
| PASS with approved deviation | 19 |
| FIXED | 1 (W-1) |
| OPEN (data / owner decision, no code defect) | 3 (O-1 … O-3), each attached to a PASS-with-deviation row as well |
| Unapproved differences left | 0 |

Defects found and fixed: **1** (W-1, OTP rate-limit body). Open items: O-1 (JSON-LD `hiringOrganization.name` makes
`verify_migration` #3 fail after the Flarize import), O-2 (Flarize's "E2E Test Panel 560W" test component is ACTIVE and
wins the value-tier panel slot of the public quote), O-3 (UAT-environment limits listed under "Not tested").

## Environment

| What | Value |
|---|---|
| Platform | this branch, `manage.py runserver 127.0.0.1:18300` (`flarize.settings.dev`, **`DEBUG=False`**, `ALLOWED_HOSTS=127.0.0.1,localhost`, Redis db 12, OTP backend `fake`), database `uat_platform` |
| Platform data | `migrate`, `ensure_audit_partitions --months 3`, `seed_roles`; then runbook §7 order: `import_cms` / `import_backend` from **private** restores (`uat_src_cms`, `uat_src_backend`) of `/home/user/platform-reference/uat/legacy_{blog_cms,goldenapp}.dump`, `import_pa --only pa.kseb_fees` (B-15), `import_flarize --source-dir /home/user/flarize-main/flarize/data` → **PriceRelease #1 and PackRelease #1 published** by the import (6 KSEB fees in the release); `LEGACY_API_SHIM` switched on through `core.services.flags.set_flag` (what Studio → Settings → Flags does); outbox drained (`drain_outbox`: 804 processed, 0 failed) |
| Legacy side | the shared read-only CMS `127.0.0.1:18009` (GETs only) and a **private** main backend on `127.0.0.1:18322` over a private restore `uat_legacy_backend` (the website's forms write), started with `/home/user/legacy-env.sh` values and an unreachable outbound proxy, so its Twilio calls can never leave the machine. The shared `legacy_*` databases and the shared 18012 server were never written to or load-tested |
| Routing | the repo's own nginx maps (`deploy/nginx/legacy/{groups,routes,switch}.conf`, snippets) rendered by `tools/uat_website/gen_nginx.py` into two local nginx servers: `:18310` — every group `shim` (→ `http://api/legacy$request_uri`, api = :18300) + `/api/public/v1/`; `:18320` — every group `old` (→ private backend / shared CMS). Same origin for site and API, as on flarize.com (no CORS involved) |
| Website | two copies of `frontend/` in a scratch folder (tracked files unchanged; only an untracked `.env.local`): `npm ci` (registry reachable), `next build && next start` on `:18311` / `:18321` behind the nginx servers. `.env.local`: `NEXT_PUBLIC_API_BASE_URL=http://127.0.0.1:<nginx>/api/`, `NEXT_PUBLIC_BLOG_API_BASE_URL=http://127.0.0.1:<nginx>/studio-api/api`, `NEXT_PUBLIC_ADMIN_API_BASE_URL=…/studio-api/admin-api/`; runtime `PUPPETEER_EXECUTABLE_PATH=/opt/pw-browsers/chromium-1194/chrome-linux/chrome`, `PDF_RENDER_ORIGIN=http://127.0.0.1:<next port>` (the quotation PDF route). The BOM URLs derive from `API_BASE_URL` (`/api/` → `/bom/`), so `/bom/api/calculate/`, `quotation-settings/`, `quotation-testimonials/` went through the shim too. The site calls no `/api/public/v1/` path yet (C7 rebinding not done); nothing to point there |
| Browser | Playwright 1.56 (global) + preinstalled Chromium, headless, 1366×900 |
| Test numbers | synthetic `90xxxxxxxx` numbers, distinct per side and run; the platform used the fake OTP client (code `000000`); no SMS could be sent by either side |

Build-time data (blog `generateStaticParams`, career postings, sitemap) was fetched through each side's nginx during
`next build`; both builds produced the same 76 prerendered pages.

## How each page/flow was compared

* `tools/uat_website/crawl.mjs` — 59 URLs; per side: HTTP status, `document.title`, full `innerText`, every JSON-LD
  block, every `<meta>`, canonical, console errors, failed requests, HTTP ≥ 400 responses, and the body of every
  `/api/`, `/studio-api/`, `/bom/` response the browser received (after scrolling the page so lazy sections load).
* `tools/uat_website/flows.mjs` — 25 interactive flows, identical script on both sides (fills, clicks, uploads, downloads,
  dialogs), same captures plus a screenshot per side.
* `tools/uat_website/diff.py` — compares the two captures; volatile noise scrubbed only for ports, the per-side test
  numbers and Next.js chunk hashes; external hosts (Bunny CDN, map tiles, GTM — blocked by the sandbox egress on both
  sides alike) are ignored.
* `tools/uat_website/apisweep.py` — 23 direct requests for old URLs the site references but no page reaches in a
  browser (`pincodes`, `tariffs`, `wattages`, `batteries`, `metadata`, installation stats edge cases, `calculate-solar`
  v1, the refused write, slash-less 301).
* `tools/uat_website/replay_gets.py` — every distinct old-URL GET the platform nginx logged during the UAT (browser
  and Next.js server-side fetches: 92 URLs incl. 421 `page-content` requests collapsed to their distinct routes),
  replayed against both sides and compared as JSON.

## Page / flow matrix

Approved-deviation references: DV rows in `docs/DEVIATIONS.md`, business defaults B-n in
`docs/decisions/business-defaults.md`, parity normalisations/approvals in `tools/parity/approved.json` and
`docs/decisions/legacy-shim.md` ("Parity evidence").

### Pages

| # | Page(s) | Old URLs behind it | Result | Notes |
|---|---|---|---|---|
| P1 | `/` | page-content, faqs, calculate-solar-new | PASS | |
| P2 | `/about` | page-content | PASS | |
| P3 | `/residential` | page-content, faqs | PASS | |
| P4 | `/commercial` | page-content | PASS | |
| P5 | `/industrial` | page-content | PASS | |
| P6 | `/solutions` | page-content, faqs | PASS | |
| P7 | `/how-flarize-works` | page-content, faqs | PASS | |
| P8 | `/group-purchase` | page-content, faqs | PASS | |
| P9 | `/solar-referral-program` | page-content, faqs | PASS | |
| P10 | `/solar-warranty` | page-content, faqs | PASS | |
| P11 | `/contactus` | page-content | PASS | |
| P12 | `/faq`, `/solar-faq` (301) | faqs | PASS | |
| P13 | `/subsidy` | faqs | PASS | only the page's own countdown clock (seconds) differs |
| P14 | `/privacy`, `/terms` | page-content | PASS | |
| P15 | `/projects`, `/projects/<slug>` | page-content | PASS | shipped data |
| P16 | `/resources`, `/resources/<slug>` | page-content | PASS | shipped data |
| P17 | `/service-area/{alappuzha,ernakulam,kannur}` | page-content | PASS | map tiles unreachable on both sides |
| P18 | `/blog` | articles (build + ISR) | PASS | |
| P19 | `/blog/<slug>` × 5 published + historical alias `net-metering-explained` | articles `filters[slug]` | PASS | byte-identical text and JSON-LD |
| P20 | `/blog/{draft,archived,unknown}` | articles | PASS | 404 on both |
| P21 | `/career` | job-positions, page-content | PASS | |
| P22 | `/career/<slug>` × 5 CMS postings (4 open, 1 closed) | job-positions/`<slug>` | PASS with approved deviation | JSON-LD `hiringOrganization.name` = "FLARIZE" only on the platform: the UAT CMS has an empty `company_name`, which the Flarize import filled from `company-profile.json` (DV-144, fill only). See **O-1** |
| P23 | `/career/design-engineer-draft` (404), shipped-data postings × 2 | job-positions/`<slug>` (404 both) | PASS | |
| P24 | `/career/general-application-form` | page-content | PASS | |
| P25 | `/solar-comparison` | solar-panels `sort=topRated` (+ slash-less 301) | PASS with approved deviation | order of panels **tied** on `kerala_climate_score` (five at 96) differs, so a different panel is second; the legacy query has no tie-breaker (heap order) — parity normalisation `ties_by_id` (legacy-shim.md). Every row identical |
| P26 | `/comparison-table?ids=…` | solar-panels `ids` | PASS | |
| P27 | `/inverter-comparison`, `/inverter-comparison-table` | solar-inverters | PASS | |
| P28 | `/emi-calculator` | emi-calculator/config, emi-calculator | PASS | |
| P29 | `/advanced-calculator`, `/solar-calculator` (301) | room-sizes | PASS | |
| P30 | `/quotation`, `/quotation/v2`, `/quotation/v2-malayalam` | quotation-settings, quotation-testimonials, emi-calculator/quotation, installation-stats | PASS with approved deviation | `quotation-settings.updated_at` = import time (parity normalisation `timestamps`) |
| P31 | `/quote-analyser`, unknown URL | — | PASS | 404 both |
| P32 | `/aboutus` (301 → `/about`) | — | PASS | |
| P33 | `/sitemap.xml`, `/sitemap-0.xml`, `/robots.txt` | build-time only | PASS | same 44 URLs, robots identical; `lastmod` = build time and `next-sitemap` enumeration order differ per build |

### Flows

| # | Flow | Old URLs | Result | Notes |
|---|---|---|---|---|
| F1 | Home basic calculator, then change bill on the result | calculate-solar-new ×2 | PASS | |
| F2 | Basic calculator, pincode outside the list | calculate-solar-new → 404 `{"error":"Pincode not found…"}` | PASS | same message on screen |
| F3 | "Get quote" popup: details → lead + BOM quote + PDF download | lead-collection-home, bom/api/calculate, fe-api PDF | PASS with approved deviation | quote has no `cost_breakdown`/`totals` (**B-1**, DV-133); panel/price lines from Flarize's catalog (**B-2**, D-2) and market rate 229,000 vs 230,000 (**B-3**); lead id 10,000,000 + n (**DV-130**); PDF `UAT Tester_3kW.pdf` downloaded on both sides. See **O-2** |
| F4 | Quotation v2 (en/ml) and legacy `/quotation` rendered from the stored quote | quotation-settings, quotation-testimonials, emi-calculator/quotation, installation-stats | PASS with approved deviation | settings `updated_at` (timestamps); quote number is random per render on both |
| F5 | Advanced calculator: existing home, on-grid | calculate-solar-advanced, device-types, ev-cars, ev-scooters | PASS with approved deviation | `device-types` row order (legacy table without ORDER BY — `rows_by_id`); results identical |
| F6 | Advanced calculator: new home, hybrid (room size, devices, EVs, backup hours) | room-sizes, device-types, ev-*, calculate-solar-advanced | PASS with approved deviation | as F5; hybrid battery priced from PriceRelease #1 = legacy (verify #10 green without the rehearsal flag) |
| F7 | "Get Detailed Quote": send OTP, verify `000000` | send-otp, verify-otp | PASS with approved deviation | the legacy UAT server cannot reach Twilio (400 with the exception text, "Something went wrong"); the platform sent through the fake client, verified, recorded the QUOTE_REQUEST lead and showed "Thank You!". Provider failures are 503 `otp_unavailable` on the platform (leads-customers.md approved differences) |
| F8 | Same number asks for a code 6 times | send-otp | **FIXED (W-1)** + approved deviation | legacy: 30-day block from the first request (429, "…after 30 days…"); platform: throttles + daily cap instead of the block (leads-customers.md) — 5 sends, 6th 429. Before the fix the site showed "Please try again after **undefined** days"; now "Too many code requests. Please try again in 8 minutes or contact our team directly." |
| F9 | EMI calculator: every size tile, subsidy toggle | emi-calculator ×n | PASS | |
| F10 | Contact form | lead-collection-home | PASS with approved deviation | DV-130 id, timestamps of the created row |
| F11 | Contact form with an invalid phone | — (client-side) | PASS | |
| F12 | Footer callback form | lead-collection-home | PASS with approved deviation | DV-130 |
| F13 | Home booking form | lead-collection-home | PASS with approved deviation | DV-130 |
| F14 | Group purchase reservation | lead-collection-home | PASS with approved deviation | DV-130 |
| F15 | Referral (affiliate) application | affiliate-applications | PASS with approved deviation | DV-130 |
| F16 | Warranty service request | warranty-service-requests | PASS with approved deviation | DV-130 |
| F17 | Warranty request with the honeypot filled | warranty-service-requests → 400 "Invalid submission." | PASS | |
| F18 | Job application for a CMS posting (resume upload) | job-applications (multipart) | PASS with approved deviation | resume/portfolio paths and download URLs null (private files — `tools/parity/approved.json` `forms/job_applications/valid_`); DV-130; JSON-LD as P22 |
| F19 | General application | job-applications | PASS with approved deviation | as F18 |
| F20 | Job application submitted empty | — (client-side) | PASS | |
| F21 | Solar comparison: filters, pick two, compare | solar-panels (filters) | PASS with approved deviation | the preselected pair follows P25's tie order |
| F22 | Inverter comparison: filters, table | solar-inverters | PASS | |
| F23 | Comparison table by ids | solar-panels `ids` | PASS | |
| F24 | Direct sweep of referenced-but-unrendered old URLs (23 requests) | pincodes, tariffs, wattages, batteries, metadata, installation-stats, calculate-solar, … | PASS with approved deviation | 21 identical; `pincodes` identical as a set (`rows_by_id`), `POST /api/tariffs/` 401 → 405 (approved.json `write-post`) |
| F25 | Replay of every distinct old-URL GET logged (92) | all read groups | PASS with approved deviation | 83 identical; 9 = P22 (5), P25 (2), P30 (1), `pincodes` order (1) |

Records written by the platform side over all runs (checked in `uat_platform`): 18 leads with the legacy sources
mapped (CONTACT_PAGE, FOOTER, GROUP_PURCHASE, HOME_BOOKING, QUOTATION, OTP-verified QUOTE_REQUEST), the affiliate
applications, warranty requests and job applications (resumes in private media) the flows submitted. No 5xx in the platform log, no
`core_system_exception` row, no parked outbox event.

## Migration checks on the UAT database

`verify_migration --source cms --source backend --cms-url … --backend-url … --legacy-cms-api http://127.0.0.1:18009`:
#1, #2, #4, #5, #6, #7, #10 (1,228 recorded calculator requests, 0 differ — **without** `--list-prices-as-release`,
because PriceRelease #1 is published), #12 PASS; **#3 FAIL**: 5 job-position differences, all
`$.meta.schema.hiringOrganization.name only in the new payload` (O-1).
`verify_migration --source flarize --flarize-dir … --offline`: #8 PASS (11 packs equal the reference, BLOCK 0), #9
PASS, #12 PASS; #6 FAIL only because the UAT did not run `--send-reset-links` (the cutover does; no mail was wanted
here).

## Defects

### W-1 — OTP rate limit shown as "Please try again after undefined days" (FIXED)

* **Seen**: F8. The website's quote popup (`src/components/SolarCalculator/QuotePopup.tsx`) handles a 429 by showing
  `errorData.message`, falling back to "…after `${days_remaining}` days…". The shim's throttled `send-otp` /
  `verify-otp` answered DRF's `{"detail": "Request was throttled…"}`; the service's daily cap and too-many-wrong-codes
  answered `{"error": …}` — neither has `message`/`days_remaining`.
* **Fix** (`legacy/views/forms.py`, `_OtpView`): every 429 of the two OTP views answers the legacy send-otp shape
  `{"error": "rate_limit_exceeded", "message": …, "days_remaining": 1}`; a throttle names the wait in minutes and sets
  `Retry-After`; the daily cap / wrong-code limit keep the service's message. `days_remaining` is 1 because the longest
  platform block is the rolling-day cap.
* **Tests** (`legacy/tests/test_forms.py`): `test_otp_throttled_answers_the_legacy_rate_limit_body_the_website_renders`
  (send and verify), `test_otp_daily_cap_answers_the_legacy_rate_limit_body`,
  `test_otp_too_many_wrong_codes_answers_the_legacy_rate_limit_body`, `test_otp_rate_limit_message_names_the_wait`.
  Re-verified after merging the final review (Indian-mobile-only `send_code`, throttles that fail open on cache errors
  only): the site shows the new message.

## Open items

* **O-1 — `hiringOrganization.name` / verify #3 after the Flarize import.** The legacy CMS prints its `company_name`
  as the JobPosting's hiring organisation and drops it when empty (the UAT CMS). The CMS import leaves the platform's
  trade name empty, and `import_flarize` then fills it (DV-144) — so after the runbook §7 order `verify_migration` #3
  reports the 5 postings. On production the CMS `company_name` is expected to be set, so the CMS import fills the trade
  name first and the difference disappears unless the two spellings differ (Flarize has "FLARIZE"). Before C1: check
  the production CMS `company_name`; if it is empty, either accept the (better) platform JSON-LD as an approved
  difference or run `verify_migration --source cms` before `import_flarize`. No code change made.
* **O-2 — test component in the Flarize catalog reaches the public quote.** `catalog.json` holds "E2E Test Panel 560W"
  (brand "E2E", ACTIVE, APPROVED, tiers base/value, ₹12,000). After the Flarize import (D-2/B-2) it is the cheapest
  value-tier DCR panel, so `POST /bom/api/calculate/` (value, 3 kW on-grid) — and the downloaded quotation — lists it as
  the panel. Business action before C5 (when `bom_calculate` moves to the shim): archive the component in Studio (or
  remove it from `catalog.json` before the cutover import). The platform imports faithfully; no code change made.
* **O-3 — not testable here** (see below); to be covered by the staging run.

## Not tested, and why

* Real OTP delivery and a real Twilio failure mode: never send SMS in UAT (fake provider on the platform; the legacy
  side has no outbound network). C2 verification "OTP works on a real phone" stays with staging.
* Images and third-party scripts (Bunny CDN, map tiles, GTM/GA): blocked by the sandbox egress on both sides; images
  come from the site's own `next.config` hosts, not from the backends, so they do not depend on the cutover.
* Studio (`/studio/**`): out of scope — no shim by design (C3 flips Studio to `/api/v1/`).
* `/api/revalidate` webhook: the platform posts to `company_profile.blog_revalidate_url`, empty in the UAT data; the
  outbox events were drained without a target. Staging must set the URL and publish an article (PLAN §8 acceptance).
* The legacy success path of send/verify-otp (it cannot reach Twilio in UAT), so F7 compares the platform success
  against the legacy failure; the shim's success body is covered by `legacy/tests/test_forms.py` against the recorded
  legacy contract.
* Load: never load-test the shared legacy servers; performance gates are k6 on staging (PLAN §8).

## Repeating the UAT on staging

Prerequisites: a staging database restored from the production dumps and imported in runbook §7 order (with
`--send-reset-links` only when intended), `LEGACY_API_SHIM` on, and the legacy containers running on **private copies**
of their databases (the forms write). Tools: `tools/uat_website/` (needs Node 22 with Playwright ≥ 1.56 and a
Chromium; Python for the diff scripts).

```bash
export UAT_DIR=/srv/uat/website            # scratch: website copies, nginx configs, logs, captures
REPO=$(pwd)
# 1. nginx pair from the repo's own maps (edit the upstream ports at the top of gen_nginx.py for staging hosts)
python tools/uat_website/gen_nginx.py "$REPO" "$UAT_DIR"
# 2. two website copies, one .env.local each (tracked files untouched)
for side in legacy:18320 platform:18310; do n=${side%%:*}; p=${side##*:}
  cp -a <frontend checkout> "$UAT_DIR/web-$n" && rm -rf "$UAT_DIR/web-$n/.next" "$UAT_DIR/web-$n/node_modules"
  printf 'NEXT_PUBLIC_API_BASE_URL=http://127.0.0.1:%s/api/\nNEXT_PUBLIC_BLOG_API_BASE_URL=http://127.0.0.1:%s/studio-api/api\nNEXT_PUBLIC_ADMIN_API_BASE_URL=http://127.0.0.1:%s/studio-api/admin-api/\n' $p $p $p > "$UAT_DIR/web-$n/.env.local"
done
# 3. one side at a time (memory): start its servers, build, capture, stop
python tools/uat_website/services.py start platform nginx-platform           # api + nginx
(cd "$UAT_DIR/web-platform" && npm ci && npx next build)
python tools/uat_website/services.py start next-platform
cd "$UAT_DIR" && UAT_SIDES=platform node $REPO/tools/uat_website/crawl.mjs 0:30 && UAT_SIDES=platform node $REPO/tools/uat_website/crawl.mjs 30:60
UAT_SIDES=platform UAT_RUN=1 node $REPO/tools/uat_website/flows.mjs         # or one flow name per call
python tools/uat_website/services.py stop next-platform nginx-platform platform
#    …then the same with legacy-backend / nginx-legacy / next-legacy and UAT_SIDES=legacy
# 4. compare
python tools/uat_website/diff.py page -v; python tools/uat_website/diff.py flow -v
python tools/uat_website/apisweep.py; python tools/uat_website/replay_gets.py
```

Expected result: only the differences of the matrix above (all approved, plus O-1/O-2 if the data are unchanged).
