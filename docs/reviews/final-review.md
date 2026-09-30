# Final cross-app review before cutover (review/final)

Scope: the fully integrated backend at `claude/bold-goodall-lxgwep` 11711b8, looking for what the per-package reviews
could not see — behaviour across apps, release safety, money parity after the merges and the honesty of the suite.
Time-boxed to high-severity issues. Every fix below has a failing test first; the tests are kept.

## What was checked and found sound

* **Cross-app registries, in a fresh (non-test) process.** 38 event types have handlers, every cross-context event
  of PLAN §3.5 that has a consumer today is wired (quotations.accepted → agreements, agreements.issued/superseded →
  site_inspections, site_inspections.released → projects, pricing.release_published → packs + website revalidation,
  procurement.batch_committed → inventory, attendance.punches_ingested, hr.employee_deactivated → accounts,
  quotations.issued → leads). The punch sink, the eight customer-merge dependants (every foreign key to
  `customers_customer` in the project — none is missing), the seven timeline providers, the three document-access
  rules and the five reserved private-media folders are all registered from `ready()`.
* **Scope across apps.** Every timeline provider and dashboard counter applies its own module's record scope; every
  cross-app uid lookup in a write (quotation → customer, agreement → customer / quotation version / base agreement,
  inspection → customer, lead convert → customer) goes through the caller's scope; documents inherit the owning
  record's visibility; the media library never lists, signs or edits files in another context's reserved folder;
  project and pack responses null landed cost and margin without `pricing_internal.view`; the public BOM quote and
  its legacy shim strip `cost_breakdown`/`totals` (B-1); the public installations payload carries no name, phone or
  address.
* **Event handlers.** Idempotent on replay (agreement per accepted version, inspection per agreement uid, project per
  inspection, stale-draft marker per price release, batch booking per batch); a merged customer is followed through
  `merged_into` by the consumers; parked events surface in `/healthz` (outbox check) and the weekly ops report, and
  `drain_outbox --requeue-parked` re-drives them.
* **Release safety.** Fresh-database `migrate` of the whole project; `migrate <app> zero` and back for every app with
  raw SQL (attendance, audit, devices, engineering, inventory, pricing, site_inspections); the `deploy/release.sh`
  migrate-phase steps in order, twice (idempotent) — once single-role, once with a separate owner role and app role
  set up as docs/ops/audit-log.md says. With the two roles, `audit_log`, `inventory_movement` and
  `attendance_raw_punch` refuse UPDATE/DELETE to the app role after migrate, after zero-and-back and after the
  maintenance commands, and the app role can still seed roles and write audit rows. Every beat entry names a
  registered task (`core/tests/test_celery.py`). Production settings: DEBUG off, docs 403 outside the allow-list,
  CORS closed by default without credentials, secure cookies, HSTS, SSL redirect (iclock and healthz exempt), no
  admin, OTP/media/renderer/e-mail on their real backends.
* **Money parity.** The golden suites run in full with Node on PATH, nothing skipped: bom 1,077 captured cases
  (1,059 × 200, 9 × 400, 9 legacy 500s now validation errors, asserted by count), packs release, quotations
  orchestrator, engines core (2,109) / commercial golden (1,910) / rules / attendance / inspection parity,
  calculators, EMI and attendance. No skip or xfail marker narrows them; the only conditional skips need Node or the
  Flarize sources, both present here.
* **Suite honesty.** No `xfail`; the five conditional skips are environment-only (Node/Flarize sources, Chromium,
  nginx binary) and one CMS case covered elsewhere. The thirteen test functions without a literal `assert` all
  assert through a helper or test "does not raise" on purpose. Autouse fixtures that switch things off (legacy
  throttles, the null punch sink in devices' tests) are paired with dedicated tests of what they switch off.

## Findings and fixes

### F-1 (high) — production could start without the app role, silently leaving the append-only ledgers writable

`flarize/production.py`. The append-only guarantee of `audit_log`, `inventory_movement` and `attendance_raw_punch`
is a `REVOKE UPDATE, DELETE, TRUNCATE` from `DB_APP_ROLE`. When `DB_APP_ROLE` is empty the migrations and the
release's maintenance commands print "privileges left unchanged" and exit 0, so a missing line in `.env` /
`owner.env` would ship a production database whose audit trail and stock ledger the application can rewrite, with a
green release. The runbook lists the variable as required, but nothing enforced it.
**Fix:** the production guard refuses to start when `DB_APP_ROLE` is empty (prod and staging); `.env.example` says
so. **Test:** `core/tests/test_settings_prod.py` (`DB_APP_ROLE` cases; the complete-environment imports set it).

### F-2 (high) — one-time codes could be texted to foreign and landline numbers from the site-inspection approval flow

`leads/services/otp.py`, `site_inspections/services/approvals.py`. The website OTP endpoints and the legacy form
shim only accept Indian mobile numbers, but `send_code` itself trusted its caller. The customer approval flow
(`/api/customer/v1/inspection-approvals/<token>/send-otp/`, anonymous with the link) sends to the number fixed when
staff requested the link — the customer record's phone (which staff may enter in any country and as a landline) or a
number a Project Head types. That opened the SMS-pumping / toll-fraud path the public endpoints were built to close,
and a landline could never receive the code anyway.
**Fix:** `send_code` — the single SMS choke point — refuses anything that is not an E.164 Indian mobile number
(400 `otp_phone_unsupported`, before the provider is called), whichever caller chose it; requesting an approval link
for such a number is refused up front (400 `phone_not_indian_mobile`, the paper approval stays available).
**Tests:** `leads/tests/test_otp.py::test_send_code_never_texts_a_number_that_is_not_an_indian_mobile`,
`site_inspections/tests/test_approvals.py::TestIndianMobileOnly`.

### F-3 (medium) — an EXTRA_STRUCTURE agreement of another customer could clear an inspection's additional work

`agreements/services/registrations.py::extra_structure_problem`. The COST_CALCULATED validator checked kind, status
and that the agreement's `source_uid` names the inspection, but not that the agreement belongs to the inspection's
customer; `create_blank` accepts any `source_uid`. An extra-structure agreement priced and issued to customer A could
therefore move customer B's walkway/extra-structure work to COST_CALCULATED — billed to the wrong customer.
**Fix:** the validator also requires the same customer (a merge repoints both records, so it stays true after one).
**Test:** `agreements/tests/test_site_inspection_wiring.py::test_an_extra_structure_agreement_of_another_customer_is_refused`.

### F-4 (medium) — the throttle's fail-open swallowed every exception, and its clock broke under test ordering

`flarize/throttles.py`, `leads/views/throttles.py`. Rate limiting fails open on a cache outage by design, but the
`except Exception` wrapped the whole DRF throttle, so any defect inside it switched rate limiting off for every
public write, OTP and login with only a warning. It showed up in this review as an order-dependent failure: DRF
stores `time.time` on the throttle class at import, and when that import happened inside a `freeze_time` block the
class kept freezegun's function, every throttle call raised a TypeError, and the fail-open quietly disabled
throttling for the rest of the session (the EMI throttle test failed when the parity suites ran first).
**Fix:** only errors raised by the cache fail open (the two cache calls are wrapped and re-raised as
`CacheUnavailable`); the clock is `time.time()` read per call. **Tests:** `core/tests/test_throttles.py`
(`test_a_cache_outage_fails_open`, `test_a_throttle_defect_is_not_mistaken_for_a_cache_outage`,
`test_the_timer_reads_the_clock_at_call_time`).

### F-5 (low) — `manage.py check --deploy` was not clean under production settings

`flarize/settings/prod.py`. Two warnings: W003 (no CSRF middleware) and W021 (no HSTS preload). W003 does not apply:
there is no session middleware and nothing authenticates by cookie (JWT, service tokens and signed links are read
from the request), so there is no ambient credential to forge a request with. W021 is an owner decision (below).
**Fix:** both silenced in prod.py with the reason, so the release gate can run `check --deploy --fail-level WARNING`.
**Test:** `core/tests/test_settings_prod.py::test_check_deploy_passes_under_production_settings`.

## Open items (not fixed here)

* **Project references are free uids.** `POST projects/` accepts `site_inspection_uid`, `agreement_uid`,
  `quotation_version_uid` and `lead_uid` without checking that they exist or belong to the project's customer
  (Project Head only). A wrong inspection uid blocks that inspection's auto-created project with
  `inspection_has_project`. Low severity; validate against the owning tables when projects is next touched.
* **Component usage ignores sales record scope.** `GET catalog/components/<uid>/usage/` (catalog.view) lists the
  numbers of issued quotations and agreements naming the component whatever the caller's quotations/agreements
  scope. Numbers only, no customer data; low.
* **`packs.release_published` → website revalidation** (PLAN §3.5) is not wired: no website page reads pack prices
  yet (calculators and EMI use their own tables, B-6). Wire it when the website adopts `/api/public/v1/packs/`.
* The UAT agent's findings on the running server (port 18300) are outside this review.

## Decisions for the owner

1. **HSTS preload** (`SECURE_HSTS_PRELOAD`, check W021). Recommendation: turn it on only once every subdomain of the
   production domain serves HTTPS, then submit the domain to the preload list; until then it stays off.
2. **Field Engineers see every customer.** The seeded Field Engineer role has `customers.view` with the default
   `all` scope (PLAN §3.2: "customer look-up"), i.e. names and phone numbers of every customer. Recommendation: keep
   it for cutover (engineers look customers up on site) but consider an `assigned`-style customers scope for field
   staff later.
3. **Automatic project creation on release** (`PROJECTS_AUTO_CREATE_ON_RELEASE`, D-8) is off in production: a
   released inspection creates no project until someone creates it. Recommendation: switch it on once Project Heads
   agree, the handler is idempotent and tested end to end.
4. **Public BOM lines keep `unit_price`/`amount`** (B-1 kept `bom_lines` byte-identical). These are list prices, not
   landed cost, so no cost or margin leaks; the website reads only name, quantity and unit. Recommendation: leave as
   is at cutover, drop the price fields from the public body when the legacy shim is retired.
