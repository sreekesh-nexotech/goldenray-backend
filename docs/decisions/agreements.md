# WP agreements — purchase agreements, sale orders, extra-structure agreements

Work package *agreements* builds the `agreements` app of PLAN §2.6 (`agreements_*`), §3.4 (Agreements), §3.5
(`quotations.accepted` consumed, `agreements.issued` / `agreements.superseded` emitted), §7.5 (the Purchase Agreement
import), D-1 (flag `AGREEMENTS_PRICE_OVERRIDE`), D-11 (`AGR-<FY>-nnnn`) and DV-3, following the design of Plan 2 §3.1.
The legacy source is the Purchase Agreement page (`agreement-goldenray-main/index.html`: `FORMS` 1/2/3, `buildDoc`
EN/ML/HI, hybrid detection, payee and letterhead; `api/products.js`: the Upstash catalog). The package builds on
`wp/quotations` (merged into this branch, see "Merges"). Deviations: DV-122 … DV-128.

## What exists

| Area | Where | Notes |
|---|---|---|
| Tables | `agreements/models/` | `agreements_agreement`, `agreements_line`. Every enum has a `CHECK` (kind, status, language en/ml/hi, system type ON_GRID/HYBRID, phase, variant, inverter type, source type, accepted via). Partial uniques: live number, live `legacy_ref`, one ISSUED and one DRAFT per (quotation version, kind) (PLAN's PU plus the draft one), one live revision per `supersedes`. Row rules: `source_type`/`source_uid` together and never with a quotation version (DV-3), legacy rows have no quotation, positive capacity, non-negative money, `final = original + extra − discount` (legacy rows exempt), an ISSUED/ACCEPTED/SUPERSEDED row is frozen (number, payload, SHA-256, `issued_at`) and a DRAFT is not, ACCEPTED has `accepted_at` + `accepted_via`, CANCELLED has `cancelled_at`; lines: description, positive quantity, non-negative money. |
| Services | `agreements/services/` | `agreements` (from quotation, `quotations.accepted` draft, blank, DRAFT edit, supersede, cancel), `issuing` (issue, render, document lookup), `acceptance` (paper acceptance, price override), `pinning` (values from an issued quotation version; catalog component columns), `fees` (KSEB registration fee), `document` (frozen document + event payload), `registrations`, `scoping`, `legacy_import`, `common`. |
| Document | `agreements/templates/documents/agreement/{en,ml,hi}.html`, `_document.html`, `_paragraph.html`, `_kseb.html`, `_styles.html`, `templatetags/agreement_document.py` | the page's `buildDoc` ported per kind × language (texts copied verbatim), rendered by `documents` from the frozen payload; the template tag only holds the three label sets and Indian digit grouping. |
| Staff API | `agreements/views/agreements.py`, `price_override.py` | table below. |
| Registrations | `agreements/services/registrations.py` | record scope `agreements.owned`; customer merge dependant (blocks customer delete); timeline provider `agreements.agreements`; documents access `agreements.agreement` (visible = the agreement is in the reader's scope); media usage (acceptance scan) and the reserved private folder `agreements/acceptance`; catalog usage `agreements.issued`; dashboard counters `agreements` (drafts, issued, accepted). |
| Events | `agreements/events.py`, `services/*` | consumes `quotations.accepted`; emits `agreements.created`, `.revision_created`, `.issued`, `.superseded`, `.accepted`, `.cancelled`. |
| Import | `agreements/services/legacy_import.py` | `import_pa_agreements(records, profile=…)`, `report_pa_catalog(doc)`. |
| Fixtures / golden | `agreements/tests/fixtures/export_pa_records.mjs` → `fixtures/pa/flarize_agr.json`, `fixtures/pa/catalog.json`, `golden/builddoc.json` | the real page's JavaScript run in Node (see "Parity evidence"). |

### Endpoints (`/api/v1/`)

| Path | Permission |
|---|---|
| `GET agreements/` (`status`, `kind`, `customer`, `owner`, `quotation`, `source_uid`, `legacy`, `search`, `ordering`), `GET agreements/<uid>/` (lines, frozen payload, signed acceptance-scan link) | `agreements.view`, scope `all`/`owned` |
| `POST agreements/from-quotation/` (`quotation_version_uid`, `kind` PURCHASE_AGREEMENT/SALE_ORDER, `language`) | `agreements.create` (+ the quotation must be in the reader's quotations scope) |
| `POST agreements/` (blank SALE_ORDER / EXTRA_STRUCTURE: `customer_uid`, `source_type`/`source_uid`, `base_agreement_uid`, equipment uids, size, prices, `lines`) | `agreements.create` (customer in the reader's customers scope) |
| `PATCH agreements/<uid>/` (DRAFT; `expected_version`) | `agreements.edit` |
| `POST agreements/<uid>/issue/` · `…/supersede/` (`language`, `quotation_version_uid`) | `agreements.issue` |
| `POST agreements/<uid>/cancel/` (`reason`) | `agreements.edit`; an issued/accepted one also `agreements.manage` |
| `POST agreements/<uid>/record-acceptance/` (multipart `file`, `note`) | `agreements.edit` |
| `GET agreements/<uid>/document/?language=` (single-use signed link) · `POST …/render/` (`language`, body or query) | `agreements.view` · `agreements.edit` |
| `POST agreements/<uid>/price-override/` (`discount` or `final_price`, `reason`) | `agreements.manage`, flag `AGREEMENTS_PRICE_OVERRIDE` (404 while off) |

## Decisions

1. **Prices are pinned, never typed, for quotation-derived agreements.** `create_from_quotation` (and the
   `quotations.accepted` handler) copy from the ISSUED version: system type (ONGRID → ON_GRID), size, phase, tier
   (→ `variant`), roof type and structure template (`flatRoof` → `flat_roof`), and — from the frozen document's locked
   BOM (`payload.bomSummary.rows`, `componentId` = catalog SKU) — panel (component, quantity, `moduleWatt`, DCR),
   inverter or micro inverters (type MICRO / HYBRID for a hybrid system / STRING), battery and the structure material.
   Prices: `original_price` = customer total incl. GST and transport, `discount` = offer + approved discounts,
   `final_price` = the version's final price, `extra_cost` = 0. A PATCH of any pinned field is 400 `field_pinned`;
   only the price override (below) moves a price. Missing equipment is left blank and reported at issue.
2. **The KSEB registration fee comes from pricing.** The KSEB_REGISTRATION band with the smallest `capacity_kw_max`
   covering the plant (an exact-phase row beats an any-phase row) — from the quotation's PriceRelease
   (`payload.statutory_fees`) when it pins one, else from the current `pricing_statutory_fee` rows (PriceRelease #1 from
   the Flarize data pins none). A blank Sale Order re-resolves it whenever its size or phase changes; Extra Structure
   agreements carry none (the page's form 3 had no fee).
3. **Blank agreements** (DV-3): a SALE_ORDER or EXTRA_STRUCTURE DRAFT for a customer the user can see. Equipment is
   chosen as catalog components (the category's BOM role must match); `base_agreement_uid` copies plant, equipment and
   base amount from another agreement of the same customer. An Extra Structure agreement names what it prices as
   `source_type = SITE_INSPECTION` + `source_uid` and has ≥ 1 line (`additional_work_item_uid` per line); its
   `extra_cost` is the sum of the lines (never typed). The typed amount of a blank agreement is its source price
   (there is no quotation to pin); everything is audited.
4. **Issue** (all or nothing): the fields of the page's form of that kind must be present (FORMS 1/2/3 mapped to
   columns; PA also needs the customer's name, phone and address) — 422 `agreement_incomplete` listing each; a
   quotation-derived draft must still point at the quotation's ISSUED version of an ISSUED/ACCEPTED quotation (409
   `quotation_version_superseded` / `quotation_closed`); then the number (`core.sequences` AGR, taken inside the
   transaction), the deep-frozen document + SHA-256, the render job in the agreement's language, the audit row and the
   events. Every issue emits `agreements.issued`; a revision also emits `agreements.superseded` (same payload).
5. **Supersede** makes a new DRAFT revision (`revision` + 1, `supersedes` → the old one, lines copied, optionally
   re-pinned to another ISSUED version of the same customer's quotations); the old agreement stays in force until the
   revision is issued, then becomes SUPERSEDED in the same transaction. Each issued revision has its own number.
6. **The document** is data only: `document.build` computes once what `buildDoc` computed in JavaScript (hybrid
   detection `/hybrid/i` on the size label or a hybrid inverter or the system type; the plant description; `Single
   Phase`/`3 Phase`; YES/NO flags) and freezes the letterhead (company name, phone, e-mail, address, website, logo),
   the payee (`M/s <legal name>`) and the primary bank account from the company master (D-9; the page hard-coded Golden
   Ray's). The templates print the page's texts verbatim in en/ml/hi. Additions, marked `.platform-extra`: the
   agreement number/date line, the payment details (bank) block and the priced lines of an Extra Structure agreement.
   A Sale Order / Extra Structure paragraph prints `net_price` (original − discount; for imported rows the typed
   amount). Amounts are printed with Indian grouping (`₹ 3,35,000/–`; the page printed the typed digits).
7. **Render and documents.** `render/` renders the frozen payload in any of en/ml/hi (`reuse=True`: an identical
   rendering is returned, never duplicated); a DRAFT renders its current document unfrozen (a preview). `document/`
   returns a single-use link to the newest non-failed rendering of the current document in that language (409
   `document_not_ready` otherwise). Document access follows the agreement's record scope.
8. **Paper acceptance** (D-13): an ISSUED agreement becomes ACCEPTED via PAPER; the scan (PDF → DOCUMENT, image →
   PHOTO, sniffed from the bytes) is stored private in the reserved folder `agreements/acceptance` before the
   transaction and discarded if the write is refused; the detail returns a 10-minute signed link. OTP acceptance is an
   enum value only (no customer surface in this package).
9. **Price override** (D-1 / D2-1): flag on (else 404), `agreements.manage`, a reason, a DRAFT; send `discount` or
   `final_price` and the other follows (`final = original + extra − discount`, both ≥ 0); audited with before/after
   and the reason, kept in `price_override_reason`.
10. **Cancel** needs a reason; a DRAFT with `edit`, an issued or accepted agreement also with `manage` (DV-127).
11. **Owned scope** (DV-122): Sales Executives see the agreements they own; the quotation's owner owns a derived
    agreement, the creator a blank one; the handler-drafted PA belongs to the quotation's owner.
12. **`quotations.accepted` handler**: a DRAFT Purchase Agreement for the accepted version unless a live, not
    cancelled PA exists for it (idempotent, at-least-once safe; the draft partial unique closes the race); unknown or
    no-longer-issued versions are logged and skipped.

## Event contract (`agreements.issued` / `agreements.superseded`)

Pinned by `agreements/tests/test_events.py` (keys, types, no commercial key), agreed with site_inspections
(`docs/decisions/site-inspections.md` on `wp/site-inspections`):

```json
{"agreement_uid", "number", "kind", "version", "customer_uid", "lead_uid|null", "quotation_uid|null",
 "system_type": "ON_GRID|HYBRID", "size_kw", "phase": "1P|3P|null", "tier": "BASE|VALUE|PREMIUM|null", "issued_at",
 "supersedes_uid|null",
 "fields": {"consumer_number", "registered_phone", "wheeling_required", "address", "pincode", "district", "location",
            "panel_uid", "panel_capacity_w", "inverter_uid", "battery_uid", "structure_type", "structure_material",
            "quotation_version_uid"}}
```

For `agreements.superseded`, `agreement_uid` is the new agreement in force and `supersedes_uid` the replaced one.

## Legacy mapping

### Purchase Agreement page `flarize_agr` records → `agreements_agreement`

| Page | Platform | Notes |
|---|---|---|
| `{id}` + export profile (`crs`/`admin`) | `legacy_ref = <profile>/<id>`, `number = <PROFILE>-<id>` | legacy map `PA flarize_agr <profile>/<id>`; the page numbered nothing (D2-2) |
| `type` 1 / 2 / 3 | `kind` PURCHASE_AGREEMENT / SALE_ORDER / EXTRA_STRUCTURE | other values: `invalid_record` |
| `createdAt` | `issued_at`, `created_at`, `updated_at` | bare dates (demo records) accepted |
| `id` `a1` … `a5` (the page's `seed()` demo records) | — | `demo_record_not_migrated` |
| `data.name` / `customerName`, `data.phone`, `data.address` | `customer_id`: `match_or_create_by_phone` (source PA_IMPORT, `customer_created`); no usable phone → a customer of its own, remembered in the legacy map `PA flarize_agr.customer` (`customer_without_phone`) | forms 2/3 have no phone field |
| `quoteno` | `legacy_quotation_ref = QUO-GR-AS-26-<quoteno>` | printed as QUOTATION NO |
| `kw` | `size_label` (as printed), `capacity_kw` (first `<n> KW`) | `unparsed_value` when no capacity |
| `kw` /hybrid/i or `invtype = Hybrid Inverter` | `system_type = HYBRID` (else ON_GRID) | the page's hybrid detection |
| `phase` Single Phase / 3 Phase | `phase` 1P / 3P | |
| `panel` | `panel_label`; `panel_dcr` from the `- DCR` / `- NDCR` suffix | no catalog FK (DV-124) |
| `panelcap` | `panel_capacity_label`; `panel_capacity_w` = first wattage | |
| `inverter`, `invtype` | `inverter_brand`, `inverter_type` STRING/MICRO/HYBRID | |
| `battery` (`None` = none) | `battery_label` | |
| `struct` | `structure_material` | |
| `extra`, `walkway`, `lader` YES/NO | `extra_structure`, `walkway_required`, `ladder_required` | |
| `variant` Base/Value/Premium | `variant` BASE/VALUE/PREMIUM | |
| form 1 `origprice`, `extcost`, `discount`, `total` | `original_price`, `extra_cost`, `discount`, `final_price` | typed values kept (arithmetic not enforced on legacy rows) |
| form 2 `amt`, `extcost`, `extdesc` | `original_price`, `extra_cost`, `extra_description`; `final_price = amt + extcost` | |
| form 3 `total`, `extcost` | `original_price`, `extra_cost`; `final_price = total + extcost` | |
| `kseb` (fee amount) | `statutory_fee_amount`; `statutory_fee_id`/label = the current KSEB_REGISTRATION row of that amount | |
| `addoffer` | `add_on_offer` | |
| the record | `payload.legacy_record` (+ the document built from the columns, frozen with its SHA-256) | an unchanged re-import is skipped; a changed record is rebuilt (`legacy_record_changed`) |

Not migrated: `flarize_trash` (soft-deleted in the page), `flarize_user` / `flarize_last` (sessions).

### Upstash catalog (`api/products.js`) → report only

`report_pa_catalog(doc)`: `panels_dcr`/`panels_ndcr`/`inverters` entries whose brand has no catalog component and
`kseb` bands without a current KSEB registration fee of that band and amount are listed (`not_in_catalog`);
`plants`, `capacities`, `batteries`, `structures`, `invtypes` have no master table (`listed_only`). Nothing is created
(PLAN §7.5). The fee list itself is imported by pricing (`pricing.services.legacy_import.import_pa_kseb_fees`).

## Parity evidence

* **Records exported from the real page.** The page keeps records only in the browser, so
  `fixtures/export_pa_records.mjs` runs the page's own JavaScript (`index.html`) in a Node `vm` (DOM/storage/network
  stubbed, clock frozen): the demo records it seeds into an empty store, then seven records saved through its own
  `saveAgr()` from filled forms covering all three forms, hybrid/micro/string inverters, batteries, quotation numbers,
  discounts, extras, KSEB fees and offers; and `api/products.js` served through a fake Redis for the catalog. The
  scenario customers are synthetic; the demo records' phones are masked.
* **Document text, 7 records × 3 languages (`test_document_parity.py`).** The page's `buildDoc(type, data, lang)`
  output (`golden/builddoc.json`) and the platform template rendered from the imported agreement print the same body
  text — title, every row, paragraphs, KSEB and offer boxes, extras block, NOTE clause, signature block — in English,
  Malayalam and Hindi, after normalising white space and digit grouping and the phone (typed `+91 90000 00102` vs the
  customer's `9000000102`). The letterhead (company master, D-9) and `.platform-extra` blocks are excluded by design.
* **Import** (`test_legacy_import.py`): 12 records → 7 agreements, 5 demo records reported, 7 PA_IMPORT customers,
  every typed column checked against the source values; re-run 0 created / 0 updated / 7 skipped; a changed record is
  rebuilt; profiles are separate; dry run writes nothing.
* **End to end on the real Flarize data** (`test_real_quotation.py`): a quotation priced and issued by the quotations
  services from PackRelease #1, accepted → the handler drafts the PA → issued; the PA pins exactly the locked BOM's
  panel/inverter/structure and the version's prices.

## Merges

`wp/quotations` (tip `e16b464`, reviewed) was merged right after the integration branch, as instructed. The
integration branch then moved (wave 4a, `9e3f7d1`: quotations and migration-website integrated) and was merged
again; its DV-117 … DV-121 (migration-website) kept their numbers and this package's rows were renumbered to
DV-122 … DV-128 (DEVIATIONS, this file, the `revision` help text in the model and its unreleased initial migration).

## Shared changes

* `flarize/settings/base.py` `SPECTACULAR_SETTINGS["ENUM_NAME_OVERRIDES"]`: `AgreementStatusEnum`, `AgreementKindEnum`,
  `AgreementBlankKindEnum`, `AgreementFromQuotationKindEnum`, `AgreementLanguageEnum`, `AgreementSystemTypeEnum`
  (collisions of `status`/`kind`/`language`/`system_type` once agreements joined the schema).
* `customers/tests/test_timeline.py`: the pinned provider list includes `agreements.agreements`.
* `documents/tests/test_jobs.py`: the "kind without a template" case uses AGREEMENT with a template variant agreements
  does not ship (`internal`), since agreements now ships the default templates.

## Open

* Trash/restore of agreements (Plan 2 §3.1) is not built: `cancel` covers withdrawing an agreement; restore would be
  a `manage` action on soft-deleted rows.
* OTP acceptance by the customer (`accepted_via = OTP`) needs a customer surface; only paper acceptance exists.
* The legacy import is not yet called by `migrations_tools` (`import_pa`: call with each profile's export, after
  `pricing.services.legacy_import.import_pa_kseb_fees`).
