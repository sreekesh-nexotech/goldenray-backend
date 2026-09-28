# WP leads-customers — website enquiries, OTP, affiliate/warranty forms, installations, customers

Work package `leads-customers` builds the `leads` and `customers` apps of PLAN §2.6, the Sales scopes of §3.2, the
public endpoints of §3.3 (`otp/send`, `otp/verify`, `leads`, `affiliate-applications`, `warranty-requests`,
`installations`, `installations/stats`), the staff endpoints of §3.4 (Sales: `leads/`, `customers/`), the events of
§3.5 and the importers of §7.3 (lead tables) and §7.4 (`customers.json`). Deviations: DV-17 … DV-24.

## What exists

### Tables

| Table | Model | Notes |
|---|---|---|
| `customers_customer` | `customers.Customer` | PLAN columns + `bill_cycle` (DV-18). PU(`code`), PU(`phone_e164`) where live and not blank; checks: source/bill-cycle enums, E.164 and pincode formats, lat/long ranges, bill ≥ 0, `merged_into` ⇒ soft-deleted, never merged into itself. Indexes: `owner` (FK), GIN trigram on `UPPER(name)` (the SearchFilter's `icontains` uses it; `pg_trgm` created in `customers.0001`), `created_at`. |
| `customers_note` | `customers.CustomerNote` | `customer` CASCADE, `body` (non-empty), `pinned`. |
| `leads_lead` | `leads.Lead` | PLAN columns + `form` (DV-17). Unique `number` (`L-<n>`, sequence `LEAD`, never reused); checks: kind/status/form enums, E.164/pincode formats, LOST ⇒ `lost_reason`, CONVERTED ⇒ `customer`. Indexes (status, created_at), (phone_e164, created_at), (created_at), FKs. |
| `leads_lead_note` | `leads.LeadNote` | `lead` CASCADE, `body`. |
| `leads_lead_event` *(no base)* | `leads.LeadEvent` | `lead` CASCADE, `at`, `by` (SET_NULL), `event` (CREATED, IMPORTED, UPDATED, ASSIGNED, STATUS_CHANGED, NOTE_ADDED, CONVERTED, ARCHIVED), `data`. Index (lead, at). |
| `leads_otp_request` *(no base)* | `leads.OtpRequest` | PLAN columns + `created_at`; checks purpose, E.164, `expires_at > created_at`. Index (phone_e164, created_at). The code is never stored (Twilio Verify owns it). |
| `leads_affiliate_application` | `leads.AffiliateApplication` | legacy columns (`full_name`, phone → `phone_e164`, `email` citext, `profession`, `district`) + `status` (NEW, CONTACTED, APPROVED, REJECTED) + `assignee`. |
| `leads_warranty_request` | `leads.WarrantyRequest` | legacy columns (`full_name`, `phone_e164`, `issue_type`, `description` — DV-22) + `customer` (SET_NULL, linked by phone), `system_details` (validated JSON ≤ 4 KB, depth ≤ 3), `status` (NEW, IN_PROGRESS, RESOLVED, CLOSED, REJECTED), `assignee`. |
| `leads_customer_installation` | `leads.CustomerInstallation` | legacy columns (`customer_name`, `phone_e164`, `pincode`, `address`, `capacity_kw` ← `system_size`, `installed_on` ← `installation_date`, `status`) + `district`, `system_type`, `is_showcase`, `photo` (public image, registered with `media.usage`), `assignee`. Indexes for the stats (pincode+status, district+status, installed_on) and a partial index for the public showcase list. |

### Endpoints

| Surface | Path | Permission / throttle |
|---|---|---|
| public | `POST otp/send/` `{phone, name?}` → `{status: pending, phone, expires_at}` | `otp` per phone + `otp_ip` per IP (DV-19); Idempotency-Key |
| public | `POST otp/verify/` `{phone, code, name?}` → `{status: approved, verification_token, expires_at}` | `otp_ip`; Idempotency-Key |
| public | `POST leads/` `{kind?, form?, name, phone?, email?, pincode?, district?, message?, page?, details?, calculator?, utm?, verification_token?, website}` → receipt | `public_write`; Idempotency-Key |
| public | `POST affiliate-applications/`, `POST warranty-requests/` | `public_write`; Idempotency-Key |
| public | `GET installations/?pincode=&district=` (paginated showcase map) | `public_read`; cached (`leads:installations` + `media`), ETag/304, `max-age=60` |
| public | `GET installations/stats/?pincode=` | `public_read`; cached 300 s server-side, `max-age=60` |
| staff | `leads/` list/detail/create/PATCH/DELETE, `…/assign/`, `…/status/`, `…/mark-lost/`, `…/mark-spam/`, `…/convert/`, `…/notes/` (GET, POST), `…/events/` (cursor) | `leads` view/create/edit/archive; assign = manage; notes POST = edit |
| staff | `leads/affiliate-applications/`, `leads/warranty-requests/` list/detail/PATCH/DELETE + `…/transition/`, `…/assign/` (DV-20) | `leads` view/edit/archive; assign = manage |
| staff | `leads/installations/` CRUD | `leads` view/create/edit/archive |
| staff | `customers/` CRUD, `…/merge/` (`into_uid`), `…/timeline/`, `…/notes/[<note_uid>/]` | `customers` view/create/edit/archive; merge = manage; notes = view/edit |

Record scope: `customers` owned = `owner` (notes through their customer); `leads` owned = `assignee` (notes/events
through their lead; affiliate/warranty/installation rows by their own assignee). Both registries map each model to its
owner path; an unknown model sees nothing.

### Services, registries, events

* `customers.services.phones` — the one E.164 normaliser (phonenumbers, region IN); website/OTP numbers must be Indian
  mobiles (as the legacy `^[6-9][0-9]{9}$` rule), staff may enter any valid number.
* `customers.services.merge` — **dependant registry** (`register_dependant(model, field, blocks_delete=False)`):
  merge re-points every registered FK (live and archived rows), fills the survivor's gaps, archives the source with
  `merged_into`, audits both rows and emits `customers.merged`; `blocks_delete=True` dependants make `DELETE` a 409
  `customer_in_use`. Registered today: notes, `Customer.merged_into`, `Lead.customer`, `WarrantyRequest.customer`.
* `customers.services.timeline` — **provider registry** (`@timeline.register(key, module=...)`); a provider runs only
  for users holding `<module>.view` and applies its own record scope. Providers today: the customer record (created,
  merges, notes), leads (received/converted), warranty requests.
* `leads.services.twilio_verify` — thin client (`requests`; integration → env; fail closed) and the `fake` backend
  (code `000000`, sends recorded in `SENT`). `LEADS_OTP_BACKEND` is `twilio` by default and `prod.py` refuses anything else.
* `leads.services.otp` — send (DB cap per number per day, provider call outside the transaction), verify (attempts cap
  by conditional UPDATE), the signed verification token (salted, 30 min, bound to number + purpose).
* `leads.services.intake` — website submissions (token check, kind/form resolution, payload document rules, customer
  link by phone, owner of that customer becomes assignee). `leads.services.leads` — staff workflow and conversion.
  `leads.services.workflows` — affiliate/warranty transitions. `leads.services.installations` — CRUD, showcase list,
  stats. `leads.services.pincode_directory` — the stats' pincode list (see below).
* Events emitted: `leads.created` `{lead_uid, number, kind, form, channel, customer_uid}`, `leads.converted`
  `{lead_uid, customer_uid, customer_created}`, `customers.merged` `{source_uid, target_uid, repointed}` (each with a
  dedup key). Consumed: `quotations.issued` `{quotation_uid, customer_uid}` → the customer's open leads become
  CONVERTED (system actor, idempotent).
* Dashboard counters: `leads` (new, open, affiliate_applications_new, warranty_requests_open), `customers` (total,
  new_30d), both scoped.

## Decisions not spelled out in the PLAN

1. **OTP on every phone-bearing submission.** `POST leads/` requires the token for the number it carries, for every
   kind; only CONTACT may come without a phone (then with an e-mail address). The token may be reused for further
   submissions of the same number until it expires (30 min) — the throttles bound volume; a single-use token would
   need state the PLAN table does not have. Legacy adapters may skip verification (DV-24).
2. **Website numbers are Indian mobiles only** (OTP and forms): the legacy sent SMS to any country (toll-fraud risk).
3. **Twilio text never reaches the website.** Provider errors map to `otp_unavailable` (503), `otp_limit_reached`
   (429), `otp_send_failed` / `otp_invalid` / `otp_expired` (400), `otp_attempts_exceeded` (429).
4. **Kind and form.** `form` keeps the legacy `source` (DV-17); either may be omitted; `OTHER` goes with any kind;
   Studio entries are `STUDIO`. The public serializer accepts the legacy lower-case values and labels
   (`LenientChoiceField`), so the shim renames fields but never maps values.
5. **Payload is a validated document**: `details` (legacy rule: ≤ 30 scalar fields, text ≤ 2,000, empties dropped),
   `calculator` (object ≤ 8 KB, depth ≤ 4), `utm` (known keys). Nothing in it is filtered by a list screen.
6. **Repeat enquiries are kept** (as the legacy did); a submission from a live customer's number links that customer
   and assigns the customer's owner.
7. **Conversion** matches the customer by phone only (never by name), creating one only with `customers.create`;
   the new customer is owned by the lead's assignee and remembers the lead. The response carries only a reference
   (`uid`, `code`, `name`): the matched customer may belong to another salesperson, whose record stays out of reach
   (the same rule makes `phone_taken` disclose the existing uid only to users who can see it).
8. **Merge** fills gaps, never overwrites; the source's phone goes to the survivor's empty `alt_phone`; the merge
   frees the source's number (it is archived in the same transaction).
9. **Affiliate applications and warranty requests are their own inboxes** (DV-20); nothing is mirrored into
   `leads_lead`.
10. **Stats keep the legacy semantics exactly** (district of the first listed row of a pincode, prefix fallback then
    `Kerala`, district counts over the listed pincodes of that district, completed rows only, current IST year) and
    run as one aggregate query. The pincode list is read from `reference.Pincode` (PLAN §2.8 columns `pincode`,
    `district`) through the app registry — sales may read reference data, reference must never import sales — and
    the directory is replaceable (`pincode_directory.register`); without a list, district counts use the rows' own
    `district`. The `pincode` query value is taken as typed (≤ 64 characters), as the legacy endpoint did.
11. **Public installations list** shows only COMPLETED showcase rows and never the name, phone or address.

## Legacy mapping

### `lead_collection_home` → `leads_lead` (BACKEND / `lead_collection_home`)

| Legacy column | Platform | Rule |
|---|---|---|
| `id` | `core_legacy_map.source_id` | new `number` `L-<n>` in legacy id order |
| `name` | `name` | trimmed; empty → `—` (`blank_name`) |
| `phone_number` | `phone_e164` | E.164; unparsable → empty, raw kept in `payload.details["Phone (as recorded)"]` (`unparsable_phone`) |
| `source` | `form` (upper-cased) + `kind` | footer, home_booking → HOME_ENQUIRY; contact_page, other, warranty_service → CONTACT; group_purchase → GROUP_PURCHASE; quotation → QUOTE_REQUEST; quote_request → ADVANCED_CALC; referral_partner → REFERRAL; unknown → OTHER (`unknown_source`) |
| `page` | `source_url` | |
| `details` | `payload.details` | legacy details rule; a non-object is kept as text (`invalid_details`) |
| `created_at`, `updated_at` | `created_at`, `updated_at` | preserved |
| (quote_request rows come from an approved `verify-otp`) | `otp_verified_at = created_at` | the "OTP verified flag" of PLAN §7.3 |
| — | `status` | NEW, or LOST with a reason when older than 90 days (PLAN §7.3) |
| — | `customer`, `assignee` | live customer with the same phone, and its owner |
| referral_partner / warranty_service mirrors | skipped (`mirrored_submission`) | when the affiliate/warranty row with the same phone exists within ±5 min (the `record_lead` copy); otherwise imported |

### `affiliate_application` → `leads_affiliate_application`; `warranty_service_request` → `leads_warranty_request`

| Legacy column | Platform | Rule |
|---|---|---|
| `full_name` | `full_name` | |
| `phone` | `phone_e164` | required (row skipped otherwise) |
| `email` | `email` | lower-cased |
| `profession` | `profession` | label → code (`Real Estate Agent` → REAL_ESTATE_AGENT …); unknown → OTHER (`unknown_profession`) |
| `district` | `district` | one of the 14 Kerala districts, any case (row skipped otherwise) |
| `issue_type` | `issue_type` | label → code (`KSEB / Net Metering` → NET_METERING …); unknown → OTHER |
| `description` | `description` | |
| `created_at` | `created_at`, `updated_at` | |
| — | `status` NEW, `customer` by phone (warranty) | |

### `customer_installations` → `leads_customer_installation`

| Legacy column | Platform | Rule |
|---|---|---|
| `customer_name`, `address` | same | unparsable phone noted in `address` when it is empty |
| `phone_number` | `phone_e164` | |
| `pincode` | `pincode` (+ `district` from the pincode list) | |
| `system_size` (float) | `capacity_kw` numeric(8,3) | must be > 0 |
| `installation_date` | `installed_on` | |
| `status` completed/in_progress/planned | COMPLETED/IN_PROGRESS/PLANNED | unknown → row skipped |
| `created_at`, `updated_at` | same | |
| — | `is_showcase` false, `system_type` blank | staff choose showcase rows |

### `solar_installations`, `solar_installation_new` — not installations (DV-21)

Reported row by row (`not_an_installation`), nothing written; the rows are committed as fixtures
(`leads/tests/fixtures/legacy_backend/solar_installation*.json`) for the calculators package. Successors:

| Legacy column | Successor |
|---|---|
| `power_capacity` | `packs_release_pack` size (kW) |
| `bill_range` (new) | `engines.energy` sizing from the bill (`calculators/basic`) |
| `total_cost` | PackRelease `customer_price_incl_gst` |
| `total_subsidy` | `emi_subsidy_rule` / `engines.subsidy` |
| `final_cost` (new) | price after subsidy (`engines.subsidy`) |
| `per_kw_rate` (new) | derived: price / kW |
| `area_required` | `engines.energy` area rule (80 sq ft per kW) |
| `time_to_complete` | calculator display configuration (calculators package) |
| `loan_available`, `interest_rate` (new) | `emi_interest_rate_rule` / `engines.finance` |
| `inverter_price` (new) | the inverter component's LIST price (`pricing_price`) |
| `type` (new, Residential/Commercial) | calculator input / pack segment |
| `created_at`, `updated_at` | not carried (configuration rows) |

### Flarize `customers.json` → `customers_customer` (FLARIZE / `customers` / `customerId`)

| Flarize field | Platform | Rule |
|---|---|---|
| `customerId` | `code` | kept (a new `CUST-nnnnnn` when it is longer than 24 characters or taken) |
| `name` | `name` | |
| `phone` | `phone_e164` | E.164; **matching is by phone only** (an unmapped row whose phone belongs to a live customer is linked to it, gaps filled, `matched_by_phone`); unparsable → empty, raw in `alt_phone` |
| `email` | `email` | lower-cased; invalid dropped |
| `address`, `district`, `state` | same | |
| `pincode` | `pincode` | six digits, else dropped |
| `currentBill` | `current_bill` | |
| `billCycle` | `bill_cycle` | MONTHLY/BIMONTHLY (DV-18) |
| `source` | `source` | known sources, else SALES_ENTRY |
| `createdBy` | `owner`, `created_by` | through FLARIZE / `users` in `core_legacy_map`; unmapped → empty (`unmapped_owner`) |
| `updatedBy` | `updated_by` | same map |
| `createdAt`, `updatedAt` | `created_at`, `updated_at` | preserved |

`customers.services.legacy_import.match_or_create_by_phone` is the same phone-only rule for the PA and SI importers.

## Parity evidence

Captured by `leads/tests/fixtures/capture_legacy.py` (committed) on 2026-09-28:

* `installation_stats.json` — 24 query variants (missing/empty/repeated/upper-case parameter, listed, multi-district,
  out-of-list, foreign, short, long, space-padded pincodes) against the shared UAT server (20 seeded installations,
  read-only) and against a **private** restored copy enriched by `enrich_private.sql` (8 more rows: capture-year,
  in-progress, planned, multi-district, out-of-list and foreign pincodes). `leads/tests/test_stats_parity.py` imports
  the same installations (exported read-only, masked) through `legacy_import`, installs the legacy `pincodes` table
  as the directory and asserts **every 200 body identical** and every 400 on `pincode` with the legacy message.
* `forms.json` — 42 requests to the private server: validation errors and accepted submissions of
  `lead-collection-home` (18), `affiliate-applications` (9), `warranty-service-requests` (7), `send-otp` (5),
  `verify-otp` (3), and the legacy per-IP throttle (6th warranty request → 429). The UAT legacy stack runs with fake
  Twilio credentials: every send/verify failed at the outbound proxy and the legacy answered 400 with the exception
  text (`HTTPSConnectionPool(host='verify.twilio.com' …)`); a failed send still blocked the number for 30 days (429).
  `leads/tests/test_legacy_parity.py` renames each payload as the shim will and asserts: legacy 400 ⇒ canonical 400
  naming the same fields; legacy 201 ⇒ canonical 201 storing exactly what the legacy response showed (name,
  10-digit phone, source, page, details / e-mail, profession, district / issue, description).
* Exported rows (`lead_collection_home.json`, `affiliate_application.json`, `warranty_service_request.json`,
  `customer_installations_*.json`, `solar_installation*.json`; masked consistently so mirrors still match) drive
  `leads/tests/test_legacy_import.py`, which checks each imported lead reads back as its legacy row.
* `customers/tests/fixtures/flarize/customers.json` — the 34 Flarize customers, masked by
  `export_flarize_customers.py` (valid mobiles stay valid, junk numbers kept).

Approved differences (asserted in `test_legacy_parity.py`): a leading trunk zero (`09876500018`) is accepted;
provider failures are 503 `otp_unavailable` without the exception text; the 30-day block is replaced by the
throttles + daily cap; foreign numbers are refused; `lead-collection-home` needs OTP on the canonical endpoint (DV-24
for the shim); affiliate/warranty submissions are no longer copied into the leads inbox (DV-20).

## Hand-over notes

* **Later sales packages** register their customer foreign keys in `AppConfig.ready()`:
  `customers.services.merge.register_dependant(Quotation, "customer", blocks_delete=True)` (agreements, projects,
  site inspections likewise), and their timeline entries with `@customers.services.timeline.register("quotations",
  module="quotations")` (newest first, `limit`, strictly older than `before`, own record scope).
* **quotations** emits `quotations.issued` with `customer_uid` (and `quotation_uid`); leads converts that customer's
  open leads.
* **legacy shim**: `/api/lead-collection-home/` → `intake.submit_lead(data=…, ip=…, require_verification=False)`
  (`phone_number` → `phone_e164` via `customers.services.phones.normalise_phone(…, mobile_only=True)`, `source` →
  `form`, `page` → `source_url`, `details` → `payload.details` via `intake.build_payload`); `send-otp`/`verify-otp` →
  `leads.services.otp`; `affiliate-applications`/`warranty-service-requests` → the canonical serializers accept the
  legacy payloads unchanged; responses in the old shapes use `customers.services.phones.national_digits` and the
  `get_*_display()` labels. `installation-stats` → `installations.installation_stats(pincode)` (same keys), 400 body
  `{"error": "Pincode parameter is required"}`.
* **migrations_tools**: order users → `customers.services.legacy_import.import_flarize_customers` →
  `leads.services.legacy_import.import_all` (affiliate, warranty, installations, leads, report of the sizing tables).
  Import the reference pincodes first so installations get their district.
* **reference**: when `reference_pincode` keeps per-post-office rows (multi-district pincodes), register an
  office-aware directory with `leads.services.pincode_directory.register(...)` from the leads side (exact legacy
  district counts); the default reads `reference.Pincode(pincode, district)`.
* Settings: `LEADS_OTP_BACKEND` (`twilio` | `fake`), `TWILIO_*` env fallback (the TWILIO integration wins),
  `LEADS_OTP_TTL_SECONDS`, `LEADS_OTP_MAX_ATTEMPTS`, `LEADS_OTP_MAX_SENDS_PER_PHONE_PER_DAY`,
  `LEADS_VERIFICATION_TOKEN_TTL_SECONDS`, throttle `otp_ip`.
