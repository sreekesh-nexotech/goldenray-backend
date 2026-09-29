# WP quotations — quotations, quotation content, the quotation document

Work package *quotations* builds the `quotations` app of PLAN §2.6 (`quotations_*`), §3.3 (`testimonials`), §3.4
(Sales: `quotations/…`, `quotation-content/…`), §3.5 (`quotations.*` events, `packs.release_published`), the quotation
document template (D-3), and the §7.3/§7.4 imports of Flarize `quotation-state.json`, `quotation-counter.json`, the
quotation content/masters and the main backend `bom_quotationtestimonial` / `sent_quotes`. Legacy sources: Flarize
`salesOrchestrator.js` (`orchestrateThreeTiers`, pack mode), `quotationPayload.js`, `generationGate.js`,
`quotationContent.js`, `quotationStore.js`, `server.js` sales routes (workflows spec C.1, J.3; engines spec §12–§16);
goldenray `frontend/src/components/QuotationV2` (artwork). Deviations: DV-104 … DV-116.

## What exists

| Area | Where | Notes |
|---|---|---|
| Tables | `quotations/models/` | `quotations_quotation`, `quotations_version`, `quotations_bom_snapshot`, `quotations_commercial_snapshot`, `quotations_discount_request`, `quotations_content_version`, `quotations_inclusion`, `quotations_tier_display_name`, `quotations_testimonial`, `quotations_campaign`, `quotations_email_log` *(no base)*. Every enum has a `CHECK`; lifecycle invariants are partial unique indexes: live number, live `legacy_ref`, (quotation, number), one DRAFT version per quotation, one primary snapshot per version, (version, tier) per snapshot, one PENDING discount request per version, one DRAFT and one PUBLISHED content version, live inclusion key, live (system type, tier) name and one recommended tier per system type, `email_log.legacy_ref`. Row rules as `CHECK`s: an issued quotation has a number, affiliate ref only for AFFILIATE, DISTRICT needs a district, `accepted_at` iff ACCEPTED, an issued version is frozen (payload + SHA-256 + `issued_at`) and a draft is not, a non-legacy version pins its PackRelease and PriceRelease, size-key formats, positive amounts, testimonial bills, campaign dates, e-mail log SENT has `sent_at`. |
| Orchestrator | `quotations/services/pipeline.py` | Flarize `orchestrateThreeTiers` over a PackRelease: for every tier the released pack's BOM (`engines.bom_domain` lock with the release's engineering rule set), Sales swaps validated against `engines.bom_builder.get_alternatives`, `engines.pack_pricing` with the version's roof/transport, the offer (`pricing.services.offers`), the engineering check, the commercial snapshot, energy/savings/subsidy/finance from the PriceRelease engine configurations, and `engines.quotation_payload.build_quotation_payload` + `engines.gate` (8 checks). The primary tier failing is a 409 with the Flarize code; alternatives failing are listed as `unavailable`. |
| Services | `quotations/services/` | `quotations` (create, draft edit, preview, issue, revise), `lifecycle` (accept, cancel, expire, discount requests, history, documents), `sending`, `content` (content versions, fit guard, masters), `options` (system options), `inputs` (validation against the release, context documents), `artwork` (the document view model), `scoping`, `registrations`, `legacy_import`, `common`. |
| Document | `quotations/templates/documents/quotation/{en,ml}.html`, `_document.html`, `_styles.html`, `templatetags/quotation_artwork.py` | the goldenray `QuotationV2` artwork (12 A4 pages: cover, proposal letter, daily energy, life with solar, why us, packages, inclusions, testimonials, specifications, savings/finance, what we handle, terms ×2, summary; campaign page when active) rendered by `documents` from the frozen payload; `artwork.view_model` only formats (Indian grouping, ₹, dates, bilingual labels from the content release). |
| Staff API | `quotations/views/quotations.py`, `versions.py`, `content.py` | table below. |
| Public API | `quotations/views/public.py` | `GET testimonials/`. |
| Registrations | `quotations/services/registrations.py` | record scope `quotations.owned`; customer merge dependant (blocks customer delete); timeline provider `quotations.quotations`; documents access `quotations.version` (visible = the quotation is in the reader's scope); media usage (testimonial photo, campaign image); catalog usage `quotations.issued`; dashboard counters `quotations` (drafts, issued, accepted). |
| Events | `quotations/events.py` | consumes `packs.release_published` (notice on older open drafts; issuing them is refused until refreshed). Emits `quotations.created`, `.issued`, `.revised`, `.accepted`, `.cancelled`, `.expired`, `.discount_requested`, `.discount_approved`, `.discount_rejected`, `quotation_content.published`. |
| Tasks | `quotations/tasks.py` | `quotations.tasks.expire_quotations` (Beat, daily 00:23 — `CELERY_BEAT_SCHEDULE`), `quotations.tasks.send_quotation_email` (one retry). |
| Golden | `quotations/tests/golden/generate_quotations.mjs` → `flarize_quotations.json` | the real Flarize JavaScript (`orchestrateThreeTiers` in pack mode, frozen clock) over the real data for 5 representative quotations. |
| Fixtures | `quotations/tests/fixtures/` (`export_legacy_quotations.py`) | masked: customer names/phones/addresses, homeowner names, bank details; `quotation-state.json` is a 7-quotation subset (every status, alternatives, two orphan snapshots). |

### Endpoints

| Surface | Path | Permission |
|---|---|---|
| staff | `GET quotations/` (`status`, `customer`, `owner`, `legacy`, `search`, `ordering`), `GET quotations/<uid>/` (with versions) | `quotations.view`, scope `all`/`owned` |
| staff | `POST quotations/` (customer + system inputs + `selections`, `source`, `district`, `affiliate_ref`) | `quotations.create`; `selections.validity_override_days` needs `quotations.approve` |
| staff | `GET quotations/system-options/?system_type&phase` | `quotations.view` |
| staff | `GET quotations/<uid>/versions/<n>/`; `PATCH …` (inputs, `refresh_release`, `expected_version`) | `quotations.view` · `quotations.edit` |
| staff | `POST …/versions/<n>/preview/` (payload + gate report + per-tier prices; nothing written) | `quotations.view` |
| staff | `POST …/versions/<n>/issue/` (`expected_version`) | `quotations.issue` |
| staff | `GET …/versions/<n>/document/?language` (signed link) · `POST …/versions/<n>/render/` (`language`) · `POST …/versions/<n>/send/` (`to`, `channel`, `language`) | `quotations.view` · `quotations.issue` · `quotations.issue` |
| staff | `POST quotations/<uid>/revise/` · `…/accept/` · `…/cancel/` (`reason`) | `quotations.revise` · `quotations.edit` · `quotations.edit` |
| staff | `GET/POST quotations/<uid>/discount-requests/`, `POST …/discount-requests/<uid>/approve/` · `…/reject/` | `quotations.view`/`quotations.edit` · `quotations.approve` (never one's own request: `self_action_denied`) |
| staff | `GET quotations/<uid>/history/` (versions + audit trail, ≤ 200 events) | `quotations.view` |
| staff | `quotation-content/versions/` list/detail, `POST`, `PATCH`, `POST …/fit-check/`, `POST …/publish/` | `quotation_content.view` · `edit` · `edit` · `edit` · `publish` |
| staff | `quotation-content/inclusions/`, `…/tier-names/`, `…/testimonials/`, `…/campaigns/` CRUD | `quotation_content.view` / `edit` |
| public | `GET testimonials/` (paginated, display order) | anonymous, `public_read`, cached 300 s under `quotations_testimonials` + `media` |

Internal cost and margin (`gross_margin_pct`, the commercial snapshots' `cost` domain and `margin_check`, internal
version pins, the payload's `pricing.internal` — withheld with Flarize's `projectIssuedPayloadForActor`) are returned
only to readers holding `pricing_internal.view`.

## Decisions

1. **One quotation, many versions; one DRAFT at a time.** `create` makes the quotation with version 1 (DRAFT) pinned
   to the current PackRelease, its PriceRelease and the published ContentRelease. The draft is edited with PATCH;
   `issue` freezes it; `revise` supersedes the issued version and opens the next DRAFT from the current releases
   (Flarize `createRevision`). The quotation number is taken from `core.sequences` **QUO** at the first issue and
   kept for every later version (Flarize `quotation-counter.json` continues: the import sets the counter after
   `lastNumber`).
2. **Issue is all-or-nothing.** The gate report must PASS (422 `generation_blocked` with the reasons, nothing
   written); the draft must still be on the current PackRelease (`release_superseded`) and have no pending discount
   request (`discount_pending`); the offer plus approved discounts cannot exceed the price (`reduction_exceeds_price`).
   Then, in one transaction: number, a BOM snapshot and a commercial snapshot per tier (DV-106), the engineering run
   (`engineering.services.runs.record_run`, subject `QUOTATION_DRAFT`), the deep-frozen document
   (`engines.quotation_payload.issued_document`) with its canonical SHA-256, two render jobs (en, ml), the
   `quotations.issued` event (with `customer_uid`, the version, number and final price).
3. **The printed price (D-4).** Flarize pinned the applicable offer on the commercial snapshot but printed the pack
   price (workflows spec J.3). The platform prints the offer and approved discounts as `pricing.customer.discount`
   (the customer total is unchanged; `final_price` = customer total − offer − discounts). This is the only intended
   difference in the parity test (checked separately).
4. **Discount requests** are on the DRAFT version: one PENDING at a time; a `quotations.approve` holder who is not the
   requester approves (adds to `discount_total`) or rejects (`accounts.services.authz.deny_self_action`); a request
   whose version was issued/superseded can no longer be decided (`version_not_draft`).
5. **Validity** comes from the pricing validity policy (`engines.quotation_payload.resolve_effective_validity`), frozen
   at issue; `valid_until` is its local date. `expire_quotations` (Beat, daily) moves ISSUED quotations past it to
   EXPIRED; accepting one that is past validity but not yet swept is refused (`quotation_expired`). An override of
   the validity days needs `quotations.approve` (`validity_override_denied`).
6. **Accept** (ISSUED, valid) emits `quotations.accepted` with the version, price and language — agreements drafts the
   PA from it (PLAN §3.5). **Cancel** (DRAFT/ISSUED/EXPIRED) needs a reason (kept as `lost_reason`).
7. **Sending** is e-mail only: the rendered PDF attached, through the platform's SMTP settings
   (`core.notifications.smtp_override`), logged in `quotations_email_log` (QUEUED → SENT/FAILED, one retry). WhatsApp
   has no provider in the platform (`channel_unavailable`, DV-112).
8. **Content releases.** A content version holds the bilingual content (`language_payload`, the Flarize
   `quotation-content.json` content in its `{en, ml}` form); saving and publishing run the fit guard
   (`engines.content_fit`) and refuse content that does not fit (422 `content_invalid`); `fit-check/` reports without
   refusing. Publishing freezes the four masters beside it (`release_payload`: inclusion matrix, tier display names,
   testimonials, campaigns, in the Flarize shapes) so a quotation always prints the release it pinned.
9. **Company and branding (D-9).** The company block of the payload comes from `company_profile` and the primary
   `company_bank_account`; Flarize's `quotation-branding-state.json` is reported by the importer (demo accounts are
   never migrated; real accounts must be entered as company bank accounts) and not written (DV-113).
10. **Legacy quotations are replayed, never re-derived.** Imported versions are `legacy = true` with no PackRelease;
    their `document_payload` is the Flarize frozen document byte for byte; `render/` renders that payload (the render
    job's payload hash is the stored SHA-256). A changed source document never overwrites a frozen copy
    (`frozen_document_changed`).
11. **Owned scope.** Sales Executives see the quotations they own (`owner`), the version sub-resources, the discount
    requests, documents (documents access registry) and the customer timeline follow it.

## Legacy mapping

### Flarize `quotation-state.json` → quotations

| Flarize | Platform | Notes |
|---|---|---|
| `quotations[qid]` | `quotations_quotation` (`legacy = true`, `legacy_ref = qid`) | legacy map `FLARIZE quotation-state.json <qid>` |
| `quotationNumber` | `number` (as printed, ≤ 48 chars, DV-104) | blank for a DRAFT (never issued) |
| `status` ISSUED with documents / anything else | `ISSUED` / `DRAFT` | |
| `customer.customerId` | `customer_id` via `FLARIZE customers`, else phone match (`match_or_create_by_phone`, `customer_created`) | `no_customer` when neither |
| `salespersonId`, `createdBy`, `document.issuedBy` | `owner_id`, `created_by_id`, `issued_by_id` via `FLARIZE users` | `unmapped_owner` |
| `validUntil` (ISO timestamp), `issuedAt`, `createdAt`, `updatedAt` | `valid_until` (local date), `issued_at`, `created_at`, `updated_at` | |
| `quotationSource`, `district`, `affiliateId` | `source`, `district`, `affiliate_ref` | |
| `documents[qid][i]` | `quotations_version` #`version` (`legacy = true`, no releases), `document_payload` = the document, `document_payload_sha256` = its canonical SHA-256, `gate_report` = `payload.generationGate` | `versionStatus["qid#n"]` ISSUED/SUPERSEDED |
| `system.*`, `snapshot.pack.inputs.*`, `subsidyType`, `ghsHouses`, `quotationLanguage`, `applianceRows` | version inputs (`system_type`, `tier`, `size_key`, `size_kw`, `phase`, `battery_config`, `future_size_key`, `roof_type`, `distance_km`, `vehicle_type`, `subsidy_type`, `ghs_houses`, `language`, `selections`) | |
| `payload.pricing.customer.*` | `customer_price_incl_gst`, `transport_extra`, `final_price` | |
| `bomSnapshots[id]` / `commercialSnapshots[id]` of the primary and of `alternativeOptions[]` | `quotations_bom_snapshot` / `quotations_commercial_snapshot` per tier (`is_primary`, `record` = the snapshot, `legacy_ref` = its id) | unreferenced snapshots: `orphan_snapshot` (not imported) |
| `quotation-counter.json` `lastNumber` | `core_sequence_counter` QUO `next_value = lastNumber + 1` (never moved back) | |

### Content and masters

| Legacy | Platform | Notes |
|---|---|---|
| Flarize `quotation-content.json` (versions) | `quotations_content_version` (the published one PUBLISHED, `release_payload` frozen from the imported masters) | |
| Flarize `quotation-inclusions.json` (tier columns + `_serviceMatrix`) | `quotations_inclusion` (`COMPONENT` rows keyed by the inclusion key, `SERVICE` rows `svc_<label>`) | DV-110 |
| Flarize `tier-display-names.json` | `quotations_tier_display_name` per system type (recommended tier + badge) | DV-110 |
| Flarize `quotation-testimonials.json` | `quotations_testimonial` (`show_on_website = false`, quotation-only) | a `photoUri` that is not an `https://` URL is a Flarize CMS asset: `photo_not_migrated` |
| main backend `bom_quotationtestimonial` | `quotations_testimonial` (`show_on_website = true`, capacity parsed from the label) | uploaded photo files: `photo_file_not_migrated` (media import) |
| main backend `sent_quotes` | `quotations_email_log` (`channel = LEGACY_LINK`, no version, `legacy_ref = quote_id`, `legacy_url`) | `send_quote_junk` not migrated |
| Flarize `quotation-branding-state.json` | — (report only, D-9) | `demo_branding_not_migrated`, `enter_as_bank_account` |

## Parity evidence

* **Engines (5 golden quotations).** `quotations/tests/test_orchestrator_parity.py` imports the real Flarize catalog,
  prices, pack configuration, engine configurations and quotation content through the platform importers, publishes
  PriceRelease #1 / PackRelease #1, issues the five golden quotations (on-grid 3 kW VALUE flat/residential; 5 kW 1P
  BASE sheet roof, transport beyond the included distance, Malayalam, no subsidy; 5 kW 3P PREMIUM elevated; 3 kW
  future-ready 5 kW; 5 kW VALUE group-housing subsidy with Sales appliance rows) with the frozen clock of the golden
  run, and compares with the real JavaScript's output (`generate_quotations.mjs`, reproducible —
  `test_golden_capture_is_reproducible`): for every tier the locked BOM (lines, roles, quantities, rules version,
  verdict, acknowledgements), the ISSUED commercial snapshot (cost traces, pricing, pack, offer, version pins), the
  payload sections (gate, system, inclusions, BOM rows, specifications, pricing, extras, subsidy, savings, financing,
  consumption, energy profile, appliance usage, content and derived figures, testimonials, campaign, terms, version
  keys) and every alternative's payload, and the commercial freeze — all equal. Excluded by design: identifiers minted
  per run, company/branding (D-9), release version labels, the renderer pin, the landed-cost check (DV-114); D-4 is
  asserted separately.
* **Imported quotations (byte for byte, re-render hash).** `test_legacy_import.py` over the masked 7-quotation subset:
  the stored `document_payload` serialises exactly as the source document, its SHA-256 is the source's, and
  re-rendering every issued version in both languages produces render jobs whose payload SHA-256 equals the stored
  one (the stub renderer completes each). Run on the full real `quotation-state.json` (scratch database, rolled back;
  not committed — personal data): 65 quotations imported, 64 documents byte-identical, 64/64 re-render hashes equal,
  129 BOM + 129 commercial snapshots, 20 orphan snapshots reported, re-run 0 created / 0 updated / 65 skipped; the
  counter continues at GR-9730.
