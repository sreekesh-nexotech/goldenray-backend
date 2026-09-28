# careers-reference — careers (departments, positions, applications) and reference data

Work package careers-reference builds the `careers` and `reference` apps of PLAN §2.8, their staff endpoints (§3.4
"Website content": `careers/…`, `reference/*`), their public endpoints (§3.3 `job-positions`, `job-applications`,
`reference/…`) and their legacy importers (§7.2 row 10, §7.3 `job_application`, `pincodes` … `ev_scooters`).
Deviations: DV-16 … DV-20 in `docs/DEVIATIONS.md`.

## What exists

| Area | Where | Notes |
|---|---|---|
| Models | `careers/models/`, `reference/models/` | `careers_department`, `careers_job_position` (+ `seo.models.SeoFields`), `careers_job_application`, `careers_job_application_note`, `careers_job_application_event` *(no base)*; `reference_pincode` (+ `reference_pincode_office`, DV-17), `reference_kseb_tariff`, `reference_device_type`, `reference_wattage`, `reference_room_size`, `reference_ev_car`, `reference_ev_scooter`, `reference_appliance`. Every enum has a DB check, every lifecycle invariant a partial unique index (live rows). |
| Careers services | `careers/services/{departments,positions,applications,public,sitemap,validation,legacy_import}.py` | writes audited, versioned, cache-bumping (`careers:positions`), outbox events; public payload builders; `sitemap_entries()`. |
| Reference services | `reference/services/{lists,pincodes,lookups,legacy_import}.py` | one `ListSpec`-driven CRUD for the seven flat lists; pincodes with nested offices; documented reads for other contexts. |
| Outbox | `careers/events.py` | `careers.application_received` → e-mail to `company_profile.application_notification_emails` when `notify_on_new_application`. |
| Media | `careers/apps.py` | folder `careers/applications` reserved (resumes never appear in the media library); `JobApplication.resume/portfolio` and `JobPosition.og_image` registered with `media.usage` (a resume cannot be deleted from under its application). |
| Legacy fixtures + goldens | `careers/tests/legacy/`, `reference/tests/legacy/` | capture scripts (read-only against the shared UAT servers; the write path against private copies), recorded payloads, masked rows, synthetic files. |

## Endpoints

| Surface | Path | Permission |
|---|---|---|
| staff | `careers/departments/` list/detail/create/`PATCH`/`DELETE` (409 `department_in_use` while positions reference it) | `departments` view/create/edit/archive |
| staff | `careers/positions/` list (`?status`, `department`, `employment_type`, `location`, `search`, `include_archived`), detail, create, `PATCH`, `DELETE` (only without applications) | `job_positions` view/create/edit/archive |
| staff | `careers/positions/<uid>/publish/`, `…/unpublish/` · `…/close/` · `…/archive/` · `GET …/preview/` | `job_positions` publish · edit · archive · view |
| staff | `careers/overview/` (counts; application counts only with `applications.view`) | `job_positions.view` |
| staff | `careers/applications/` list (`?status`, `position`, `assignee`, `source`, `created_from/to`, `search`, `include_archived`), detail (notes, timeline, file metadata, allowed transitions) | `applications.view` |
| staff | `careers/applications/<uid>/status/`, `…/assign/`, `POST …/notes/` | `applications.edit` |
| staff | `GET …/notes/` (page-number), `GET …/events/` (cursor), `GET …/download/<resume\|portfolio>/` (10-minute signed URL) | `applications.view` |
| staff | `DELETE careers/applications/<uid>/` (archive), `…/restore/` | `applications.archive` |
| staff | `reference/{pincodes,tariffs,device-types,wattages,room-sizes,ev-cars,ev-scooters,appliances}/` CRUD (`DELETE` = soft delete) | `reference_data` view/create/edit/archive |
| public | `GET job-positions/` (`?department=<slug>`), `GET job-positions/<slug>/` | anonymous, `public_read`, cached 300 s (`careers:positions` + `company`) |
| public | `POST job-applications/` (multipart; `Idempotency-Key`) | anonymous, `public_write` |
| public | `GET reference/{tariffs,device-types,wattages,room-sizes,ev-cars,ev-scooters,appliances}/`, `GET reference/pincodes/<pincode>/` | anonymous, `public_read`, cached 24 h (`reference:<list>`) |

Every module involved allows only the `all` scope (PLAN §3.2), so holders of a grant see every row; the tests
assert it. All actions and PATCHes take `expected_version` (409 `stale_version`).

## Decisions not spelled out in the PLAN

1. **Position workflow.** publish: DRAFT/CLOSED/ARCHIVED → PUBLISHED, refused (400 `position_not_ready`,
   `errors.publish_errors`) while the legacy publish gate lists problems (title, active department, location,
   description); the first `published_at` is kept, `closed_at` cleared. unpublish: PUBLISHED/CLOSED → DRAFT. close:
   PUBLISHED → CLOSED (`closed_at`). archive: any other → ARCHIVED. Repeating an action is a no-op; any other move is
   409 `invalid_status_transition`. Each transition emits `careers.position_<published|unpublished|closed|archived>`
   (`{position_uid, slug, status, path}`) for website revalidation; editing a live posting emits
   `careers.position_updated`. `DELETE` soft-deletes only a posting nobody applied to (409
   `position_has_applications`). `application_deadline` stays informational, as in the CMS (it feeds JSON-LD
   `validThrough`); `opens_on`/`openings` are stored and shown to the Studio.
2. **Application workflow** (PLAN statuses): NEW → SCREENING/REJECTED/WITHDRAWN; SCREENING → INTERVIEW/REJECTED/
   WITHDRAWN/NEW; INTERVIEW → OFFERED/REJECTED/WITHDRAWN/SCREENING; OFFERED → HIRED/REJECTED/WITHDRAWN/INTERVIEW;
   HIRED → WITHDRAWN; REJECTED/WITHDRAWN → SCREENING (reopening is deliberate, as in the legacy queue). Archive is the
   soft delete; an archived application accepts only `restore/` (409 `application_archived`). `assign/` links a
   posting (title/department snapshotted, the submitted `position_label` kept — legacy §6.14) and/or sets the
   assignee, who must be active and hold `applications.view` (400 `invalid_assignee`). Every change writes a timeline
   event, an audit row, and — for status moves — `careers.application_status_changed`.
3. **Public submission.** The legacy field names and rules are kept exactly (`PublicJobApplicationSerializer`,
   `careers/services/validation.py`): 10-digit Indian mobile (+91/91 stripped) stored as E.164, linkedin.com host,
   `https://` added, choice lists, 3,000-character note, declaration, honeypot `website` (400 "Invalid submission."),
   `.pdf/.doc/.docx` ≤ 10 MB. On top: the file type is sniffed from the bytes (`media`), the posting must be
   PUBLISHED (400 `position_not_open`), and title/department are snapshotted server-side. Files are PRIVATE `RESUME`
   assets in the reserved folder `careers/applications`, named `<Candidate_Name>_Resume.<ext>` so downloads keep the
   legacy names; they are discarded if the row cannot be written. `Idempotency-Key` makes retries safe. The client IP
   (trusted-proxy aware) is stored but never returned.
4. **Downloads** use the media signed URL (`media/download/<token>/`, 10 minutes, DV-13): the Studio asks
   `…/download/<kind>/` (needs `applications.view`) and follows the URL. No new token-only endpoint and no change to
   `media` was needed.
5. **Public careers payloads are the CMS contract** (`cms/careers/public.py`) with `uid` for `id` (DV-19);
   `meta.intro`/`meta.accepting_general_applications` come from `company_profile.careers_*` (the CMS `SiteSettings`
   fields); the JSON-LD site URL is `company_profile.website` and the hiring organisation its trade name (the CMS used
   `FRONTEND_BASE_URL` and `SiteSettings.company_name`). The list is unpaginated like the CMS but hard-capped at 200
   postings (`PUBLIC_LIST_LIMIT`).
6. **Reference lists.** Public lists are `StandardPagination` pages of *active* rows in `sort_order` (tariffs: slab
   order). The importer sets `sort_order` to the legacy id, so the website sees legacy insertion order (the legacy
   endpoints had no ORDER BY); staff-created rows without a `sort_order` go to the end. **The website must request
   `?page_size=200`** — every current list is shorter (device types 18, wattages 25, room sizes 7, EV cars 14,
   scooters 15, tariffs 5, appliances 11) — and read `results`. Server-side cache 24 h, invalidated by every staff
   write; `Cache-Control: public, max-age=60` (the platform budget).
7. **Tariff schedules.** `phase` null = every phase; `effective_from` null = "since before the platform". The
   schedule in force on a day is, per phase group, the rows with the latest `effective_from` not after it
   (`lookups.current_tariffs`); the public list serves today's schedule; `lookups.slab_for_units` reproduces the
   legacy `min_units__lte` lookup.
8. **Pincodes** are never listed publicly; `reference/pincodes/<code>/` returns the pincode with its post offices
   (legacy order). `lookups.district_of` / `pincodes_in_district` reproduce the installation-stats lookups
   (a pincode belongs to every district one of its offices is in).
9. **Uniqueness** is enforced by the database (live rows only) and reported as 409 (`department_name_taken`,
   `department_slug_taken`, `position_slug_taken`, `<entity>_exists`, `pincode_exists`); DRF's generated unique
   validators are disabled so the code is stable. Names that the calculators look up (device types, EV models) are
   unique case-insensitively.
10. **Shared change:** `SPECTACULAR_SETTINGS["ENUM_NAME_OVERRIDES"]` gains the careers/reference choice sets (a second
    `status` enum made drf-spectacular warn). Nothing else outside the two apps changed.

## Legacy mapping

### CMS `careers_department` → `careers_department` (§7.2 row 10: copy)

| Legacy | Platform | Note |
|---|---|---|
| `id` | `core_legacy_map` (CMS, `careers_department`) | |
| `name`, `slug`, `description`, `is_active`, `sort_order` | same | name unique case-insensitively among live rows |
| `created_at`, `updated_at` | same | preserved |

### CMS `careers_job_position` → `careers_job_position` (§7.2 row 10: status map)

| Legacy | Platform | Note |
|---|---|---|
| `id` | `core_legacy_map` (CMS, `careers_job_position`) | the public payload's `id` becomes `uid` |
| `title`, `slug`, `location`, `experience_required`, `description`, `responsibilities`, `requirements`, `benefits`, `application_instructions`, `application_deadline`, `sort_order`, `published_at`, `closed_at` | same (DV-16) | |
| `employment_type` | same codes | `full_time` … |
| `status` draft/published/closed/archived | DRAFT/PUBLISHED/CLOSED/ARCHIVED | unknown → violation |
| `department_id` | `department` | through the department map; missing → violation |
| `seo_title`, `meta_description`, `canonical_url`, `schema_type`, `schema_extra`, `noindex` | same (`SeoFields`, DV-15) | |
| `og_image_id` | `og_image` | through the CMS `media_asset` map; missing → imported without, reported |
| `created_by_id`, `updated_by_id` | `created_by`, `updated_by` | through the CMS `accounts_admin_user` map (accounts importer) |
| `created_at`, `updated_at` | same | preserved |
| — | `opens_on`, `openings`, `og_title`, `og_description` | PLAN additions |

### Main backend `job_application` (+ `_note`, `_event`) → `careers_job_application` (+ note, event)

| Legacy | Platform | Note |
|---|---|---|
| `id` | `core_legacy_map` (BACKEND, `job_application`) | |
| `position` (free text) | `position_label` | |
| `position_id` (CMS id, no FK) | `position` (FK) | through the CMS position map; unmapped → snapshot kept, reported |
| `position_title`, `department_name` | same | snapshot |
| `status` new/reviewing/interview/selected/rejected | NEW/SCREENING/INTERVIEW/OFFERED/REJECTED | DV-18 |
| `status_changed_at` | same | |
| `archived_at` | `deleted_at` | archive = soft delete |
| `full_name` | `name` | |
| `email` | `email` | trimmed, lower-case |
| `phone` (10 digits) | `phone_e164` | `+91…`; invalid → violation, skipped |
| `location`, `linkedin`, `portfolio_website`, `current_company`, `current_role`, `availability`, `declaration_accepted` | same | |
| `total_experience`, `relevant_experience`, `current_salary`, `expected_salary`, `notice_period`, `heard_about_us` | same values | unknown choice → blank, reported |
| `cover_note` | `cover_letter` | |
| `resume`, `portfolio_file` (FileField paths) | `resume`, `portfolio` (PRIVATE `RESUME` media) | bytes read from the `backend_media` volume, sniffed, checksum verified; a changed file on re-import replaces the asset; missing/refused → imported without, reported |
| `created_at` | `created_at`, `updated_at` | preserved |
| — | `source` = IMPORT, `ip` null, `assignee` null | |
| note `author`, `body`, `created_at` | `author_name`, `body`, `created_at` | |
| event `kind` received/status/assigned/archived/restored/note | RECEIVED/STATUS/ASSIGNED/ARCHIVED/RESTORED/NOTE | `from_status`/`to_status` mapped like `status` |
| event `detail`, `actor`, `created_at` | `detail`, `actor_name`, `created_at` | |

### Main backend reference tables → `reference_*` (§7.3: copy)

| Legacy | Platform | Note |
|---|---|---|
| `pincodes` (`pincode`, `state`, `district`, `office_name`, `region`, `division`, timestamps) | `reference_pincode_office` (one per row, mapped) + `reference_pincode` (one per code) | pincode `district`/`state` = lowest-id office; `serviceable` true, `distance_km_from_office` null |
| `kseb_tariffs` (`min_units`, `max_units`, `rate`) | `reference_kseb_tariff` (`slab_from_units`, `slab_to_units`, `rate_per_unit`) | `phase` null, `effective_from` null, `fixed_charge` 0 |
| `device_types` (`name`, `show_in_ui`, `url`, `watts`, `k_value`) | `reference_device_type` | `url` null → "" |
| `wattages` (`value`, `show_in_ui`) | `reference_wattage` | |
| `room_size` (`bhk_type`, `size`, `units`) | `reference_room_size` | no legacy timestamps |
| `ev_cars`, `ev_scooters` (`model`, `battery_capacity`, `claimed_range`, `adjusted_real_world_range`, `ex_showroom_price`, `energy_consumption`, `k_value`) | `reference_ev_car`, `reference_ev_scooter` | price → `numeric(14,2)` |
| Flarize `quotation-content.json` `appliances.master` (`id`, `name{en,ml}`, `icon`, `watts`, `defaultHours`, `optional`) | `reference_appliance` (`code`, `name`, `name_ml`, `icon`, `watts`, `default_hours`, `is_optional`) | FLARIZE map; `sort_order` = list position |
| every table | `is_active` true, `sort_order` = legacy id | |

Every importer returns `{created, updated, skipped, violations}`, is idempotent through `core_legacy_map`, keeps
rows deleted in the platform deleted, preserves source timestamps, and writes one `audit_log` row per batch
(`careers.legacy_imported` / `reference.legacy_imported` with counts and the batch sha256).

## Parity evidence

| What | How | Result |
|---|---|---|
| `GET job-positions/` and `/<slug>/` = CMS `/api/job-positions` and `/<slug>` | `careers/tests/test_public_parity.py`: goldens recorded by `careers/tests/legacy/capture_cms.py` from the shared CMS on :18009 (`cms_shared.json`, 11 responses) and from a private CMS copy with edge cases on :18161 (`cms_enriched.json`, 17 responses: SEO/`noindex`, `schema_extra` trying to override facts, blank lines and `\r\n` in lists, inactive department with a live posting, closed-only department, sort ties, company name/intro set); the same rows imported through `legacy_import`; every response replayed | identical (status and body) except `id` → `uid` (DV-19); 404s answer in the platform envelope |
| `POST job-applications/` = legacy `POST /api/job-applications/` | `careers/tests/test_application_write_parity.py`: 32 cases recorded by `capture_applications.py` against a PRIVATE restored `legacy_goldenapp` (`legacy_goldenapp_careers_reference`, private server :18162, media and logs in scratch, throttle lifted); same fields and file bytes replayed | 29 cases identical in outcome, error fields, error messages and stored values; 1 (`position_id` not a number) identical except the message text, because the field now holds a uid; 2 approved differences: content sniffing refuses a text file named `.pdf` (`unsupported_file_type`), and a CLOSED posting refuses applications (`position_not_open`) |
| Reference payloads | `reference/tests/test_parity.py`: rows + `/api/<list>/` responses recorded by `reference/tests/legacy/capture_backend.py` from :18012 (6 lists in full, 215 post-office rows of 32 pincodes incl. every multi-district/-division pincode, Flarize appliance master) | every legacy field present with the same value (tariff columns under the PLAN names); legacy-id order |
| Importers on the recorded data | `careers/tests/test_legacy_import.py`, `reference/tests/test_legacy_import.py` | counts, idempotent re-runs, updates, deleted-stays-deleted, violations, file checksums |

## Hand-over notes

* **Legacy shim** (`/legacy/api/job-positions`, `/legacy/api/job-applications/`, `/legacy/api/<list>/`): build
  the old shapes from `careers.services.public` / the reference serializers, map `uid` ↔ integer through
  `core_legacy_map` (`CMS careers_job_position`, `BACKEND <table>`), translate the form's integer `position_id`
  the same way, and wrap errors as `{"message": "Validation failed", "status": "error", "errors": …}`.
* **Website rebinding**: `job-positions` → read `uid` instead of `id` and post it as `position_id`; reference lists
  → `?page_size=200` and `results`; pincodes are only looked up one at a time.
* **Calculators / leads / installations** read reference data only through `reference.services.lookups`.
* **SEO** `sitemap/entries` should include `careers.services.sitemap.sitemap_entries()`.
* **Revalidation**: subscribe to `careers.position_*` events (payload carries `path`).
* Recapturing goldens: see the docstrings of the three capture scripts; never point the write-path capture at the
  shared `legacy_goldenapp` (the script refuses).
