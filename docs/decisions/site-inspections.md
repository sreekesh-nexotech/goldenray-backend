# WP site-inspections — field inspections, customer location approval, agreement linking, V2 import

Work package `site-inspections` builds the `site_inspections` app of PLAN §2.7, the Site-inspection block of §3.4
(staff and customer surfaces), the `agreements.issued` / `agreements.superseded` consumers and the
`site_inspections.released` producer of §3.5, and the Site Inspection V2 importer of §7.5. The detailed design is
Plan 2 §3.2. The readiness and checklist rules are the engines-ops package's (`engines.inspection_readiness`,
`engines.inspection_checks`); this package feeds them the current rows. Deviations: DV-122 … DV-127.

## What exists

### Tables

| Table | Model | Notes |
|---|---|---|
| `site_inspections_inspection` | `Inspection` | Every PLAN column, typed and grouped by wizard stage, with **no commercial column**. `number` is unique (`SV-YYYYMMDD-NNNN`). CHECKs cover every enum (4 required, 20 optional), the E.164 phone, the pincode, lat/long ranges, percentages 0–100 and 24 non-negative measurements. Other CHECKs: AGREEMENT origin ⇒ `agreement_uid`; ON_HOLD ⇒ reason and `held_from_status`; INSTALLATION_READY ⇒ `released_at`. Partial unique: one live inspection per `agreement_uid`. Indexes: (engineer, status), (status, visit_date), created_at, and every FK. Agreement and quotation references are uid columns (DV-122). Extras: `held_from_status`, `agreement_number` (DV-123). |
| `site_inspections_snapshot` | `Snapshot` | Versioned `number` (unique per inspection), `source` PRE_SALE/AGREEMENT, `agreement_version`, `data`. Written only by services. |
| `site_inspections_photo` | `Photo` | A private `media_asset` (PROTECT) in the reserved folder `site-inspections/<inspection uid>`. Fields: `photo_type` (10 values), `stage` 1–10, `captured_at` (EXIF), `caption`. Unique (id, inspection) is the target of the annotation composite FK. |
| `site_inspections_annotation` | `Annotation` | `geometry {x,y,w,h}` in image space, `geometry_space` IMAGE/LEGACY_CONTAINER, measurements, `number`, `is_current`. Partial unique: one current per (inspection, type). **Composite FK** `(photo_id, inspection_id) → photo(id, inspection_id)`, added by migration 0002 as raw SQL. |
| `site_inspections_equipment_assessment` | `EquipmentAssessment` | `checks_version` is pinned. `results` holds every check of the definition. `status` is recomputed. Review fields: `review_status`, `resolved_by/at`, `resolution_note` (a CHECK requires the note and time for RESOLVED/WAIVED). Partial unique on (inspection, type). |
| `site_inspections_engineering_review` | `EngineeringReview` | `trigger` (MANUAL, EQUIPMENT, STRUCTURE, ROOF, GENERATION), `decision`, `reviewer`, `decided_at` (CHECK). Partial unique: one PENDING per inspection. |
| `site_inspections_observation` | `Observation` | `stage`, `category`, `note`, photos M2M (`site_inspections_observation_photo`). |
| `site_inspections_location_approval` | `LocationApproval` | PLAN columns plus `paper_reason` (DV-124). Partial uniques: one PENDING per inspection, and the token hash. CHECKs: PENDING ⇒ link hash and expiry; a paper approval ⇒ scan and reason; E.164 phone; hex hash. |
| `site_inspections_additional_work_item` | `AdditionalWorkItem` | 13 work types. `agreement_uid` points to the EXTRA_STRUCTURE agreement (DV-122). Partial unique on (inspection, work_type). CHECKs: a costed or quote-sent item ⇒ `agreement_uid`; a decided item ⇒ `decided_at`. |

### Endpoints

All staff paths are under `/api/v1/`. Module `site_inspections`. Record scope: all, owned (customer's owner, or the
creator), or assigned (engineer).

| Path | Action | Notes |
|---|---|---|
| `GET site-inspections/` / `GET …/<uid>/` | view | Scoped. Filters: status, system_type, origin, complexity, engineer, unassigned, customer, agreement, visit_from/to. Search and ordering. No prices anywhere. |
| `POST site-inspections/` | create | PRE_SALE only. The customer must be visible to the caller. Assigning an engineer here also needs `assign`. |
| `DELETE site-inspections/<uid>/` | archive | Soft delete. Refused (409) once APPROVED or later. |
| `PATCH …/<uid>/stages/<stage>/` | edit | One view per stage, each with its own allow-list serializer; an unknown key is a 400. Stages: `site-access`, `roof-structure`, `shading`, `electrical`, `cable-routing`, `additional-work`, `engineering-review`. |
| `POST …/assign/` · `start/` · `submit/` | assign · edit · submit | |
| `POST …/hold/` · `resume/` · `revision/` · `system-type/` | approve | |
| `POST …/release/` | release | Emits `site_inspections.released`. |
| `POST …/approval/request/` | submit **or** approve | Checked in the service. Returns the customer link once. |
| `POST …/approval/paper/` | approve | D-13: multipart with `scan` and `reason`. |
| `GET …/approvals/` | view | |
| `GET/POST …/photos/`, `DELETE …/photos/<uid>/` | view / edit | Multipart upload. Responses carry signed URLs. |
| `GET/POST …/annotations/` | view / edit | `?current=true`. |
| `GET …/equipment/`, `GET/PUT …/equipment/<type>/` | view / edit | |
| `POST …/equipment/<type>/review/` | approve | RESOLVED or WAIVED, with a note. |
| `GET/POST …/observations/` | view / edit | |
| `GET/POST …/reviews/` | view / edit | |
| `POST …/reviews/<uid>/decide/` | approve | |
| `GET/POST …/additional-work/`, `PATCH/DELETE …/additional-work/<uid>/` | view / edit | |
| `POST …/additional-work/<uid>/transition/` | edit to re-identify, approve for everything else | Checked in the service. |
| `GET …/readiness/` · `activity/` (cursor) · `snapshots/` | view | |
| `GET …/report/?variant=customer\|internal` | view | Returns 202 with a render job (200 when the job is reused and already done). |
| `GET engineer/site-inspections/` | view | The caller's assigned work, ordered: in progress, draft/revision, awaiting customer, completed, other. |

Customer surface: `/api/customer/v1/inspection-approvals/<token>/` `GET`, `POST send-otp/`, `POST respond/`. Throttle
scope `customer`. No session. Every response is `private, no-store`. The token in the path is already redacted in
access logs (`flarize.logging.redact_path`).

### Services, registries, events

* `services/common.py`
  * Querysets and record scope.
  * Write-window guards.
  * The single audit writer: every write is recorded on the inspection object, so `activity/` is `audit.history`.
  * `build_state()` feeds the readiness engine from the current rows. It is used for submit, release and the `readiness/` view.
* `services/stages.py`: the ten stages and their allow-lists.
* Workflow and records services:
  * `inspections.py`: create, stage save, assign, start, system type, archive, and `send_back()`.
  * `lifecycle.py`: submit, hold, resume, revision, release.
  * `photos.py`, `annotations.py`, `equipment.py`, `reviews.py`.
  * `work.py`: observations, and the additional-work lifecycle with `register_agreement_validator()`.
  * `approvals.py`, `snapshots.py`, `report.py`.
* `services/linking.py`: the handlers of the agreement events (below).
* `services/customer_timeline.py`: timeline provider `site_inspections`. It lists created, released and the customer's answers, all scoped.
* `services/legacy_import.py`: the V2 importer.
* `AppConfig.ready()` registers:
  * the reserved folder `site-inspections`;
  * `media.usage` entries for photo assets, approval signatures and paper scans;
  * a customer-merge dependant (it blocks deleting a customer);
  * `documents.access` for `site_inspections.inspection` (report jobs follow the inspection's record scope).
* Template `templates/documents/inspection_report/{customer,internal}/en.html`, rendered by the documents Playwright pipeline.

## Event contracts

### Consumed: `agreements.issued` and `agreements.superseded`

Both events use the same payload. The agreements package should emit exactly this:

```json
{
  "agreement_uid": "uuid — the agreement this event is about (for superseded: the NEW agreement in force)",
  "number": "AGR-2026-27-0001",
  "customer_uid": "uuid — customers_customer.uid (a merged customer is followed to its survivor)",
  "lead_uid": "uuid | null",
  "quotation_uid": "uuid | null",
  "system_type": "ON_GRID | HYBRID",
  "size_kw": "5.000 (string or number)",
  "phase": "1P | 3P",
  "tier": "BASE | VALUE | PREMIUM",
  "issued_at": "ISO-8601",
  "supersedes_uid": "uuid of the agreement this one replaces | null",
  "kind": "PURCHASE_AGREEMENT | SALE_ORDER | EXTRA_STRUCTURE (optional, default PURCHASE_AGREEMENT; EXTRA_STRUCTURE is ignored)",
  "version": "positive int (optional; default 1, or the linked agreement_version + 1 when superseding)",
  "fields": {
    "consumer_number": "KSEB consumer number",
    "registered_phone": "any spelling; stored E.164",
    "wheeling_required": "true | false",
    "address": "…", "pincode": "6 digits", "district": "…", "location": "…",
    "panel_uid": "catalog component uid", "panel_capacity_w": 545,
    "inverter_uid": "catalog component uid", "battery_uid": "catalog component uid (HYBRID)",
    "structure_type": "…", "structure_material": "…",
    "quotation_version_uid": "uuid | null"
  }
}
```

Every key in `fields` is optional, and unknown keys are ignored. The payload never carries prices. A malformed event
(missing or invalid uid, `system_type` not ON_GRID/HYBRID, unknown customer, `fields` not an object) raises
`PayloadError`. The outbox retries it and then parks it, which makes it visible in `/healthz` and the ops report.

Handling (`linking.apply_agreement`, idempotent on `agreement_uid`):

* **Link.** Pick the customer's most recent live PRE_SALE inspection that has no agreement and is not
  APPROVED, INSTALLATION_READY or REJECTED. The match is by customer FK only.
  * It is converted in place: origin becomes AGREEMENT; the agreement uid, number and version and the system type
    are set; the `quoted_*` columns are written.
  * Engineer-verified fields are filled **only where empty**: consumer number, registered phone, wheeling, phase,
    address, pincode, district, location.
  * A new AGREEMENT snapshot is added. The audit action is `linked_to_agreement`.
* **Create.** If no PRE_SALE inspection qualifies, a new DRAFT AGREEMENT inspection is created with the agreement's
  system type (never UNDECIDED). `pre_sale_source` is set to the customer's last pre-sale visit, if there is one.
* **Refresh.** When `supersedes_uid` names the agreement an inspection is linked to, the inspection is updated:
  * it moves to the new uid, number and version, and gets a new snapshot;
  * a changed system type resets the checklists that no longer apply;
  * once the inspection is COMPLETED or later, it is sent back to REVISION_REQUIRED and its approvals are superseded.

### Emitted: `site_inspections.released`

Payload: `{inspection_uid, number, customer_uid, agreement_uid|null, quotation_version_uid|null, system_type,
size_kw|null (the quoted size, a decimal string), phase|null (1P/3P; NC is sent as null), lead_uid (always null: an
inspection is not linked to a lead), released_at, released_by_uid}` — the size/phase/lead keys were added at the
wave 4b integration to meet the projects contract (docs/decisions/projects.md "Event contracts"). Dedup key: `site_inspections.released:<uid>:<version>`. Consumer: projects (D-8).

## Decisions not spelled out in the PLAN

1. **Write windows.**
   * The engineer's data can be written only in DRAFT, IN_PROGRESS and REVISION_REQUIRED. The first write moves the
     inspection to IN_PROGRESS and needs an assigned engineer.
   * After submit, the record waits for review and approval. Sending it back is `revision/`.
   * Decisions (engineering reviews, equipment waivers, additional-work transitions, hold) stay open until
     INSTALLATION_READY.
   * APPROVED and INSTALLATION_READY are read-only (409 `inspection_read_only`). This is enforced in the services for
     every write: stages, photos, annotations, equipment, observations, work items.
   * DV-126 covers the COMPLETED lock.
2. **Stage allow-lists are strict.** A key outside the stage is a 400 naming the key; it is never silently dropped.
   The following are never writable through a stage: `status`, `system_type`, `origin`, the agreement columns,
   `engineer`, `customer`, `number`, `visit_date`, `quoted_*`, and the layout columns.
3. **Layout from rectangles.** The current annotation is the only writer of `panel_photo`, `panel_width_m`,
   `panel_height_m`, `panel_area_m2` and the equipment equivalents. `area = width × height` (V2 allowed the two to
   diverge). Each save keeps history (`number`, `is_current`).
4. **Engineering reviews.** A review is created when a trigger condition *becomes* true: complexity, structure
   decision, roof condition, generation impact, or a checklist turning critical. Only one review is pending at a time;
   later triggers are appended to its reason.
   * The engineer cannot set complexity back to ROUTINE while a review is pending (409 `review_pending`).
   * A ROUTINE decision sets complexity to ROUTINE.
   * REQUIRES_CHANGES keeps readiness blocked until a new review is requested and decided.
5. **Equipment.**
   * `PUT` is a full replace validated against the pinned definition.
   * A FAIL needs an evidence photo from the same inspection, and a FAIL or REQUIRES_REVIEW needs an `issue`.
   * A critical status sets `review_status=PENDING`. Changed answers re-open a waiver.
   * Resolving is `approve`: RESOLVED or WAIVED, with a note.
6. **Customer approval.**
   * The request needs `submit` or `approve`, and a SUITABLE or CONDITIONAL site. It stores the location snapshot and
     supersedes an earlier pending link.
   * The link token is 256 random bits. Only its SHA-256 is stored, and it expires after 7 days
     (`SITE_INSPECTIONS_APPROVAL_TTL_DAYS`).
   * The link is returned once to the requester, who sends it to the customer. Twilio Verify carries only the code
     (`leads.services.otp`, purpose APPROVAL, to the approval's phone). See DV-126.
   * The code goes to a number on the customer record (`phone_e164`, then `alt_phone`). Because the requester
     receives the link, an engineer cannot choose another number (403 `phone_not_customer`) — otherwise they could
     route the code to themselves and approve for the customer. Only `approve` (Project Head, who can also record a
     paper approval) may use another number or fall back to the KSEB registered phone; the number is audited.
   * `respond/` verifies the code before anything is written. It then records the decision, comment, optional drawn
     signature (private SIGNATURE asset) and the trusted client IP.
   * Only the latest PENDING, unexpired approval of an inspection that is CUSTOMER_APPROVAL_PENDING can be answered.
     A rejection needs a comment.
   * Staff can never answer on the customer's behalf; the D-13 paper route is separate, audited, and requires the scan.
   * A photo shown in any approval's location snapshot keeps its file when the photo is deleted later (the photo row
     is soft-deleted; the private asset is the approval's evidence).
   * An inspection ON_HOLD is protected as the status it was held from: it cannot be archived when held from
     APPROVED/INSTALLATION_READY, an agreement does not convert it, and an agreement's system-type change while it is
     held after completion supersedes its approvals and makes `resume/` return to REVISION_REQUIRED.
7. **Release** evaluates readiness on the current row: annotations, approvals with snapshot comparison, checklists,
   work items and reviews. Customer-impacting work blocks release until APPROVED or NONE (D-12).
8. **Additional work.** The seven-state map is enforced as in V2 `validateAdditionalWorkTransition`.
   * Identifying an item and editing or deleting an IDENTIFIED item are the engineer's actions (`edit`); every other
     move needs `approve`.
   * COST_CALCULATED needs the EXTRA_STRUCTURE `agreement_uid`. `work.register_agreement_validator(fn)` lets the
     agreements package verify it; until one is registered, any uid is accepted.
9. **Scopes.** `owned` means the inspections of customers I own, plus the ones I created. `assigned` means
   `engineer = me`. Child rows are only ever reached through a visible inspection.
10. **Report.**
    * One template per variant. The internal variant adds remarks, cable details, complexity, readiness blockers,
      reviews and per-check results.
    * Photos are embedded as 480 px thumbnail data URIs within a 700 KB budget, so the renderer fetches nothing.
    * Rectangles are drawn from image-space geometry. LEGACY_CONTAINER rectangles are flagged as approximate.
    * An identical payload reuses its job. The filename is the V2 one.
11. **Numbering.** `next_number("SV")` runs in the create transaction on the office-local day. There is no preview
    endpoint. The importer moves each day's counter past the imported numbers.

## Legacy mapping (Site Inspection V2 SQLite → platform)

### `site_inspections` → `site_inspections_inspection`

| V2 column(s) | Platform | Rule |
|---|---|---|
| `id` | `core_legacy_map` (SI / `site_inspections`) | natural fallback: `site_visit_no` |
| `site_visit_no`, `site_visit_date` | `number`, `visit_date` | SV counter continued per day |
| `customer_id`, `engineer_id` | `customer`, `engineer` | via the customers / engineers maps; an unmapped customer ⇒ row skipped |
| `purchase_agreement_id`, `_version` | `agreement_uid` = uuid5(`SI_AGREEMENT_NAMESPACE`, `PA:<id>`), `agreement_version` | the agreements importer must use the same uid for its PA rows |
| `inspection_origin` | `origin` | AGREEMENT when a PA id exists, else PRE_SALE |
| `system_type`, `inspection_snapshot_json` | `system_type` | the column if ON_GRID/HYBRID, else the snapshot's `systemType`/`system.type`, else UNDECIDED |
| `inspection_snapshot_json` | `site_inspections_snapshot` #1 `{"legacy": …}` | source AGREEMENT/PRE_SALE |
| `status` | `status` | unknown ⇒ DRAFT (violation); ON_HOLD ⇒ reason "Imported on hold", held from DRAFT; INSTALLATION_READY ⇒ `released_at` = `approved_at` (else `updated_at`) |
| `complexity_status`, `complexity_reason` | same | enum-validated |
| location/access/roof/shading enums (`road_access`, `roof_*`, `direction_facing`, shading, `generation_impact`, `tariff`, `distribution_board`, `earthing`, `site_structure_type`) | same names | unknown ⇒ blank + `invalid_enum` |
| `vehicle_type` (labels) | `vehicle_type` | Car/Pickup / Van/Truck/Manual movement only/Not assessed → CAR/PICKUP_VAN/TRUCK/MANUAL/NOT_ASSESSED |
| `phase` (free text) | `phase` | three/3… → 3P, single/1… → 1P, "not…" → NC |
| `neutral_link`, `termination_point` | each `normalise_availability` | the legacy stored one collapsed value in both |
| `connected_load`, `sanctioned_load` (TEXT) and every REAL measurement | `*_kw`, `*_m`, `*_m2` | parsed; negative/garbage ⇒ null |
| `shading_percentage`, `suitability_percentage` | `shading_pct`, `suitability_pct` | > 100 ⇒ null + `out_of_range` |
| `final_recommendation` + `site_suitable_for_solar` | `site_suitability` | `normalise_suitability` (CONDITIONAL kept) |
| `registered_phone` | `registered_phone_e164` | E.164 or blank + `unparsable_phone` |
| `wheeling_required` Yes/No, flag columns "No"/"YES"/1 | booleans | legacy `asBoolean` (+ PARTIAL = true) |
| `has_location_restrictions` | bool | legacy `asBoolean` |
| `quoted_solar_size`, `quoted_panel_capacity`, `quoted_structure_*` | `quoted_size_kw`, `quoted_panel_capacity_w` (digits), same | brands/types stay in the legacy snapshot |
| `quoted_original_price`, `quoted_discount`, `quoted_final_price` | **not imported** | `commercial_not_imported` |
| `panel_photo_id`, `equipment_photo_id` | `panel_photo`, `equipment_photo` | after the photos (`link_layout`) |
| flags `walkway_required` … `civil_work`, `additional_structure_work`, `other_additional_work` + `additional_work_*` | `site_inspections_additional_work_item` per flag | status from `additional_work_status` (NONE → IDENTIFIED; COST_CALCULATED/QUOTE_SENT → ENGINEERING_REVIEW with a violation); lengths → quantity (m) |
| `created_at`, `updated_at` | same | preserved |
| `customer_temp_id`, `sales_project_id`, `installation_readiness`, `approved_by`, `created_by`/`updated_by` pseudo-users | not imported | readiness is derived |

### Other tables

| V2 table | Platform | Rule |
|---|---|---|
| `engineers` + `engineer_users` | `accounts_user` (role `field-engineer`) | no e-mail in V2 ⇒ placeholder `<code>@site-engineers.invalid` (`email_placeholder`); active = engineer active and login active; no password (scrypt not portable) — `must_reset_password` |
| `customers` | `customers_customer` | `match_or_create_by_phone` (phone only; unmatched → new `SI_IMPORT`; unparsable phone ⇒ skipped, its inspections too) |
| `site_inspection_photos` | `media_asset` (PRIVATE PHOTO) + `site_inspections_photo` | data URL decoded and uploaded through the media pipeline (sniffed); invalid ⇒ `invalid_data_url`; types SITE_ACCESS→ACCESS, EQUIPMENT→EQUIPMENT_AREA, CABLE_ROUTING→CABLE_ROUTE |
| `site_inspection_annotations` | `site_inspections_annotation` | `{x,y,width,height}` → `{x,y,w,h}`, `geometry_space=LEGACY_CONTAINER`, `metadata_json.widthM/heightM/areaM2` → measurements, number 1, current |
| `site_inspection_equipment_assessments` | `site_inspections_equipment_assessment` | results validated (unknown dropped), status recomputed over the full definition — each change is a `status_recomputed` line (the Project Head's diff report); `resolution_status` RESOLVED/WAIVED_APPROVED → RESOLVED/WAIVED when critical |
| `site_inspection_approvals` | `site_inspections_location_approval` | `version` → `number`; PENDING → EXPIRED (no link ever existed); `otp_verified_at` null, no snapshot (readiness reports `CUSTOMER_APPROVAL_OUTDATED` until a new approval) |
| `site_inspection_observations` | `site_inspections_observation` | category label → code, stage 1–10, `photo_ids_json` → M2M |
| `site_inspection_activity` | not imported | the legacy log recorded only generic `UPDATED` lines; the platform's activity is the audit log |
| `site_inspection_materials`, `_documents`, `purchase_agreements`, `engineer_sessions` | not imported | dead tables / agreements package / sessions |

## Parity evidence

* **Schema.** No production SQLite file exists in the reference estate. `tests/fixtures/build_legacy_fixture.py`
  creates the V2 schema from the legacy source itself (read-only): the `CREATE TABLE` block of `lib/db.ts` plus every
  `ensureColumn`. The result is 131 `site_inspections` columns, matching spec §A.4.
* **Fixture.** The builder writes rows with the legacy encodings and exports `legacy_si.json`. All personal data is
  invented. The rows cover:
  * "No" strings in INTEGER columns, Yes/No wheeling, PARTIAL cabling, 1/0 suitability, collapsed neutral/termination;
  * vehicle-type labels, free-text phase, prices, container rectangles, data-URL photos;
  * a one-tap PASS checklist, WAIVED_APPROVED, and a PENDING approval;
  * a broken data URL, an unparsable phone, an out-of-range percentage and an unknown roof type.
* **Import tests.** `tests/test_legacy_import.py` imports the fixture and asserts:
  * every coerced column;
  * the violations list, including the equipment diff: legacy PASS → IN_PROGRESS for the one-tap checklist;
  * idempotence on re-run, and that the SV counter continues;
  * dry run writes nothing;
  * readiness of an imported INSTALLATION_READY row: blocked by `LOCATIONS_NOT_DOCUMENTED` (legacy rectangles) and
    `CUSTOMER_APPROVAL_OUTDATED` (no snapshot), which is the intended fail-closed outcome.
* **Readiness and checklists.** Parity against the legacy TypeScript is the engines-ops package's evidence: 55
  scenarios and 15 answer maps, re-run with every test run by `engines/tests/test_inspection_parity.py`. This package
  adds the persistence around it. The legacy Next.js app itself cannot run in the reference estate (no
  `node_modules`, `better-sqlite3` absent), so there is no live HTTP parity. The V2 HTTP contract is replaced, not
  shimmed; it is not in PLAN §6.2.
* **Engineer boundary.** `test_views_misc.py::test_engineer_boundary_no_commercial_field_is_reachable` walks every
  serializer of the app, the stage allow-lists included, plus the model's columns. It fails if a price, discount,
  cost, margin, amount, GST or payment field is reachable. It replaces `scripts/audit-v2-engineer-boundary.mjs`.

## Site Inspection V2 spec §J — disposition (package-level items; engine-level items are in engines-ops.md)

| §J | Fixed by |
|---|---|
| 1, 8, 12 unauthenticated routes, weak engineer auth | staff JWT + registry permissions + record scope; engineers are platform users |
| 2, 3, 4 body spread / forgeable readiness / blocklist PATCH | typed create serializer; readiness derived; per-stage allow-lists (400 on anything else) |
| 5 commercial leak | no commercial column exists; boundary test |
| 6 read-only not enforced | write windows in `common.py` |
| 7, 29 approval snapshot / non-transactional approval | snapshot stored at request; request, respond and paper are single transactions |
| 9 photo ownership | `photos.get_photo` everywhere + composite FK |
| 11 base64 photos | private media pipeline, signed URLs, soft delete with usage guard |
| 13, 24–27 PA linking | `linking.apply_agreement` (typed system type, fill-only-empty, audit, snapshot versions, unique per agreement) |
| 14, 15 snapshot/DTO gaps | snapshot endpoint; every column in the detail serializer |
| 21, 22 review / additional work | engineering reviews with decisions; enforced work lifecycle; D-12 gate |
| 28 create drops fields | typed columns; importer coerces the legacy encodings |
| 30 rectangles | image-space geometry, history, layout written from rectangles |
| 31–34 report | loaded equipment/observations, identified work only, measurements from metres, variants, auth + signed download |
| I (numbering) | race-free `SequenceCounter`, no overflow at 9,999, local day |

## Hand-over notes

* **agreements**
  * Emit the two events with the contract above.
  * Give imported legacy PA agreements `uid = uuid5(SI_AGREEMENT_NAMESPACE, "PA:<legacy id>")`
    (`site_inspections.services.legacy_import`).
  * Optionally register `site_inspections.services.work.register_agreement_validator(fn)` from `AgreementsConfig.ready()`
    to verify EXTRA_STRUCTURE uids (the sales apps share one layer). Until then, any uid is accepted.
* **projects**: consume `site_inspections.released`.
* **migrations_tools**
  * Run `seed_roles` first, then `site_inspections.services.legacy_import.import_all(tables)` with `tables` from the
    SQLite file (`SELECT *` per table).
  * Send the reset links to the imported engineers after fixing their placeholder addresses.
  * Give the `status_recomputed` lines to the Project Head.
* **Settings** (optional; defaults in code):
  * `SITE_INSPECTIONS_APPROVAL_TTL_DAYS` (7);
  * `SITE_INSPECTIONS_APPROVAL_LINK_BASE` (the website page that hosts the approval flow; default is the API path).
