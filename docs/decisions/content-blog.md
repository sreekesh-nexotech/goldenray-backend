# content-blog — blog (collections, templates, taxonomy, entries, delivery) and seo (metadata, redirects, overview, sitemap)

Work package content-blog builds the blog and seo apps of PLAN §2.8, the website-content endpoints of §3.3/§3.4, the
revalidation hook of §3.5 and the importers of §7.2 rows 4–7 and 11 and §7.3 (`goldenray_metadata`). It keeps the
legacy CMS logic that worked (slug validator, alias table with a partial unique index, publish-time validation against
the template, template image groups and attribute slots, the Strapi-v5-flat delivery contract) and fixes the
weaknesses listed in `cms/CMS_BLUEPRINT.md` §17. Deviations: DV-17 … DV-22 in `docs/DEVIATIONS.md`.

## What exists

| Area | Where | Notes |
|---|---|---|
| Models | `blog/models/{schema,taxonomy,entry}.py`, `seo/models/{metadata,redirect}.py` | 17 blog tables + `seo_page_metadata`, `seo_redirect` (constraints below). `SeoFields` (seo/models/fields.py) is reused by `blog_entry_seo`; its rules now live in `seo_issues_for()` so SQL and Python share them. |
| Validators | `blog/validators.py` | legacy slug validator (malformed / placeholder / reserved, same order and codes), `api_uid`, keys, path prefixes, colours, component uids. |
| Services | `blog/services/{schema,taxonomy,entries,slugs,workflow,attributes,preview,delivery,revalidation,sitemap,seo_overview,dashboard,legacy_import}.py`, `seo/services/{metadata,redirects,overview,providers,sitemap_feed,legacy_import}.py` | every write `@transaction.atomic`, explicit `user`, `check_version`, audit row, cache bump, outbox event where public pages change. |
| Staff API | `blog/views/{schema,taxonomy,entries,entry_actions}.py`, `seo/views/staff.py` | table below. |
| Public API | `blog/views/delivery.py`, `seo/views/public.py` | table below. |
| Beat | `blog.tasks.publish_due_entries` (every minute, `CELERY_BEAT_SCHEDULE["blog.publish_due_entries"]`) | publishes due scheduled entries. |
| Outbox | `blog/events.py` | `blog.entry_*`, `blog.content_changed`, `website.revalidate_requested` → revalidation client. |
| Importers | `blog/services/legacy_import.py`, `seo/services/legacy_import.py` | idempotent through `core_legacy_map`; fixtures in `blog/tests/fixtures/legacy_cms/`, `seo/tests/fixtures/legacy_backend/`. |

### Staff endpoints (`/api/v1/`, module `blogs` / `seo`)

| Path | Actions (registry action) |
|---|---|
| `content/collections/` | list/retrieve (view), create (create), PATCH (edit), DELETE (archive; 409 `collection_in_use`) |
| `content/templates/` + `…/<uid>/duplicate/` | CRUD as above; duplicate (create) copies groups and slots atomically |
| `content/templates/<uid>/image-groups/`, `…/attribute-slots/` | CRUD; keys immutable (400 `key_immutable`), unique per template (409 `key_taken`) |
| `content/authors/`, `content/categories/`, `content/tags/`, `content/badges/` | CRUD; slug generated when blank; refused while used (409 `<kind>_in_use`) |
| `content/entries/` | CRUD (view/create/edit/archive); one-call write with replace-all children; DELETE = soft delete |
| `content/entries/check-slug/` | view — availability, validity and `slug-N` suggestion (aliases count as taken) |
| `content/entries/<uid>/submit/` | edit — DRAFT → REVIEW |
| `…/publish/`, `…/unpublish/`, `…/schedule/` | publish |
| `…/archive/`, `…/restore/` | archive |
| `…/verify/` | verify |
| `…/duplicate/` | create |
| `…/preview/` | view — signed public preview link |
| `…/slug-history/` GET (view) / POST (edit), `…/slug-history/<alias_uid>/deactivate/` (edit) | aliases |
| `seo/metadata/`, `seo/redirects/` | CRUD (view / edit) |
| `seo/overview/` | view — paginated, `?kind=&seo_status=`, `counts`, `kinds` |
| `dashboard/` | `blogs` counters: `entries_draft/review/published/archived/scheduled` |

### Public endpoints (`/api/public/v1/`, anonymous, `public_read`)

| Path | Notes |
|---|---|
| `content/<collection>/` | Strapi-v5-flat list; cached 300 s server-side (60 s HTTP), namespaces `blog:{collections,templates,entries,authors,categories,tags,badges}` + `media` |
| `content/<collection>/<slug>/` | one-item list payload (alias → current slug + `meta.redirect`); 404 `entry_not_found` |
| `content/preview/<token>/` | any status, `private, no-store`, `X-Robots-Tag: noindex`; 403 `signature_invalid`, 410 `link_expired`, 404 when deleted |
| `seo/metadata/<page>/` | legacy item shape `{page, title, description, keywords, imageUrl, ogtype}` (no integer id); cached (`seo:metadata`, `media`) |
| `seo/redirects/` | `{from_path, to_path, status_code, permanent}` paginated (≤ 200/page) for the Next.js build; cached (`seo:redirects`) |
| `sitemap/entries/` | `{path, lastmod}` from every app's `services/sitemap.py`, sorted by path, paginated; cached with every provider's namespaces |

## Decisions not spelled out in the PLAN

1. **Delivery `id` values are public numbers, not primary keys (DV-17).** Byte parity needs the CMS integer ids of
   entries, authors, categories, tags, badges and content blocks. Each of these tables has a `delivery_id`
   (unique, never reused) taken from a `core.sequences` counter (`BLOG_ENTRY`, `BLOG_AUTHOR`, …) on create; the
   importer writes the CMS primary key and moves the counter past it (a clash with an existing number gets a fresh one
   and is reported). The staff API never shows them; `documentId` is the entry `uid` (= CMS `document_id`).
2. **Byte parity mechanics.** The payload is built by plain functions (`blog/serializers/delivery.py`) in the legacy
   key order; timestamps are `datetime.isoformat()` of the stored values (the importer preserves `created_at`,
   `updated_at`, `published_at`, `published_on`); JSONB bodies round-trip through Postgres in the same normalised key
   order as in the CMS. The response cache used to re-serialise payloads with sorted keys — it now keeps the view's key
   order (the ETag still hashes the canonical sorted body; shared change in `flarize/cache_utils.py`). The delivery
   views key the cache on the query string **as sent** (`ordered_query=True`): the CMS applies `sort[n]` keys in
   query-string order and a repeated `filters[f][$eq]` / `pagination[page]` uses its last value, so reordered queries
   must never share a cached body. `attributes` is delivered in insertion order (`id`), as the CMS did — never
   alphabetically.
3. **Query language kept, weaknesses fixed.** Same field map, operators, pagination defaults/clamp, `fields`
   selection and single-slug alias fallback (`$eq`/`$eqi` only, exact alias match, never on an empty `$ne` query).
   Fixed: an unparseable value on a typed column is 400 `invalid_filter` (was 500 — §17 #1); booleans accept
   `true/false/1/0/yes/no/t/f`; `$in` splits one comma-separated value (§17 #11) and understands `filters[f][$in][n]`;
   `fields=a,b` is split. Unknown fields/operators/params stay silently ignored (the security boundary).
4. **Lifecycle.** CMS `draft/published/deleted` becomes `DRAFT/REVIEW/PUBLISHED/ARCHIVED` (+ soft delete for a real
   removal, which frees the slug and retires the entry's aliases). `published_at` is set once; `published_on` is seeded
   from it when blank; unpublish keeps both; restore goes to DRAFT; publish from ARCHIVED needs a restore first.
   Publish-time validation is the legacy rule set plus a re-check of attribute values against the current slot types;
   drafts save loose but every value is typed against its template on every write.
5. **Scheduling.** `schedule/` validates for publication immediately and stores `scheduled_for` (status unchanged;
   a DB check forbids a schedule on PUBLISHED/ARCHIVED rows). The Beat task publishes each due entry in its own
   transaction under a `SYSTEM` audit context (`FOR UPDATE SKIP LOCKED`); an entry that no longer validates is
   unscheduled and audited (`blog.entry_schedule_failed`) instead of failing every minute.
6. **Services take `user` explicitly (§17 #7/#8).** Authorisation is the view's `HasModulePermission`; no service
   skips a check because `user` is `None` — the only `user=None` caller is the Beat task, under an explicit `SYSTEM`
   audit context. `duplicate/` needs `blogs.create` and resets `published_at`, `published_on`, `archived_at`,
   `scheduled_for` and the verification (§17 #6).
7. **Children are replace-all and never destroyed.** Sending `content_blocks` / `images` / `attribute_values` replaces
   the set: old rows are soft-deleted (history kept), new rows inserted; link rows (`blog_entry_category/tag/badge`) are
   plain link tables (DV-19). The SEO block is one row per entry (OneToOne), restored rather than re-created;
   `seo: null` removes it (delivery `seo: null`).
8. **Masters in use are protected.** Collections, templates, authors, categories, tags and badges used by a live entry
   cannot be deleted (409), so the payload never embeds a deleted record; `media.usage` is registered for every media
   FK (entry cover, entry images, author avatar, entry SEO image, page-metadata OG image) so media refuses deleting a
   referenced asset (§17 #12).
9. **Revalidation (§3.5, §17 #2–#4, #23).** Services emit `blog.entry_published|unpublished|archived|deleted|updated|
   slug_changed` (deduplicated per entry version) and `blog.content_changed` (a shared record shown on published pages
   changed; ≤ 100 entry paths) with the affected site paths. The handler posts **one request per path** in the
   frontend's existing contract `{"secret", "path"}` (its route calls `revalidatePath(path)`), plus
   `X-Flarize-Timestamp` and `X-Flarize-Signature: sha256=HMAC(secret, "<ts>.<body>")` for a later signature check.
   It runs after commit (outbox), never raises (fail-soft; non-2xx and network errors are logged), sends nothing
   without URL or secret, and fails closed on an undecryptable secret. `website.revalidate_requested`
   (`{"paths": [...]}`) lets pages/FAQs/careers use the same hook without importing blog; seo metadata edits use it.
   Backends: `http` (default), `fake` (tests — pinned in `flarize/settings/test.py`), `off` (`BLOG_REVALIDATE_BACKEND`).
   One call is bounded because it runs inside the outbox drainer's Celery time limit (120 s hard): the first transport
   failure ends the batch and `BLOG_REVALIDATE_BUDGET_SECONDS` (20) caps it; skipped paths are logged (ISR catches up).
   Every entry event and `blog.content_changed` also revalidate the entries' **active alias URLs**: the website renders
   the article itself under an old URL (it reads `data[0]` and ignores `meta.redirect`), so an edit, a shared-record
   change or a take-down must not linger there until the ISR window expires.
10. **Preview links.** `TimestampSigner` over the entry uid (own salt), 1 h (`BLOG_PREVIEW_TTL_SECONDS`), reusable until
    expiry, shows the current state of any status, dies with the entry. The token is a path segment, so it is
    redacted in Django and nginx access logs like the other capability tokens (shared change).
11. **SEO overview in SQL (§17 #15).** Providers (`<app>/services/seo_overview.py`: `SEO_OVERVIEW_KIND`,
    `seo_overview_rows()` built with `seo.services.overview.overview_rows`) annotate `seo_status` with a `CASE` that
    mirrors `seo_issues_for` (tested row by row against the Python rules; lengths are taken after stripping every
    whitespace character with `REGEXP_REPLACE(…[[:space:]]…)`, like Python's `strip()` — `TRIM` removes spaces only,
    so pasted newlines flipped the status at the 60/70/160 boundaries); rows are `UNION ALL`-ed, filtered, ordered
    worst first and paginated in the database; `issues` messages are computed only for the returned page; `counts` are
    `GROUP BY`. Blog contributes every live, non-archived entry (no SEO block → title fallback, missing description).
12. **Sitemap by convention.** `seo.services.providers` imports `<app>.services.sitemap` for every installed app once
    per process; each exposes `sitemap_entries()` (and optionally `SITEMAP_CACHE_NAMESPACES`). Invalid paths are
    skipped with a warning; duplicate paths keep the newest `lastmod`. Blog lists published, indexable entries of active
    collections plus each collection index (`path_prefix`, a new collection column).
13. **Page metadata.** Route keys are normalised (`/About/` → `about`, `/` → `home`); a nested key is served by
    `seo/metadata/<path:page>/`. Typed columns replace the PLAN's `og jsonb` (DV-20). Edits ask the website to
    revalidate that page. Redirects refuse self-redirects and loops (a chain is followed up to 20 hops); `hits` is kept
    for a future counter (redirects run inside the Next.js build).
14. **Shared changes (all backward compatible):** `flarize/cache_utils.py` (cached payload keeps key order; opt-in
    `ordered_query` cache key — the default key still normalises parameter order), `flarize/logging.py` +
    `deploy/nginx/flarize.conf` (redact `content/preview/<token>/`), `flarize/settings/base.py`
    (Beat entry `blog.publish_due_entries`; `ENUM_NAME_OVERRIDES` for `BlogEntryStatusEnum`, `BlogContentBlockKindEnum`),
    `flarize/settings/test.py` (`BLOG_REVALIDATE_BACKEND = "fake"`), tests in `core/tests/test_cache_utils.py`,
    `core/tests/test_middleware_logging.py`, `core/tests/test_deploy.py`.

## Legacy mapping

### CMS (`blog_cms`) → blog (`blog.services.legacy_import`)

| Source table | Source column | Target | Rule |
|---|---|---|---|
| `catalog_collection` | `api_uid`, `singular_name`, `plural_name`, `description`, `is_active`, `created_at`, `updated_at` | `blog_collection` same names | `api_uid` validated (`preview` reserved → refused, reported); `path_prefix` new: `articles` → `/blog`, else `/<api_uid>` (or the caller's map) |
| `catalog_template` | `name`, `slug`, `description`, `is_active`, `sort_order`, timestamps | `blog_template` | 1:1 |
| `catalog_template_image_group` | `key`, `label`, `repeatable`, `max_items`, `required`, `order` | `blog_template_image_group` (`order` → `position`) | `max_items` on a non-repeatable group dropped (reported; DB check) |
| `catalog_template_attribute_slot` | `key`, `label`, `type`, `options`, `required`, `order` | `blog_template_attribute_slot` | `text/richtext_blocks/number/boolean/date/enum/url` → `TEXT/RICHTEXT_BLOCKS/NUMBER/BOOL/DATE/ENUM/URL`; invalid options kept verbatim and reported |
| `catalog_author` | `id`, `name`, `bio`, `role` | `blog_author` (`id` → `delivery_id`) | `slug` generated from the name (unique) |
| `catalog_category` | `id`, `name`, `slug` | `blog_category` | a NULL slug stays NULL (delivered as `null`, as the CMS did); the staff API sets one on the next edit that sends `slug` |
| `catalog_tag` | `id`, `name` | `blog_tag` | `slug` generated |
| `catalog_badge` | `id`, `label`, `color` | `blog_badge` (`label` → `name`) | invalid colour → default `#123532` (reported); `slug` generated |
| `content_entry` | `document_id` | `uid` | unchanged (frontend `documentId`) |
| | `id` | `delivery_id` | the public Strapi `id` |
| | `collection_id`, `template_id`, `author_id` | FKs via `core_legacy_map` | unmapped template/author dropped (reported); unmapped collection refuses the row |
| | `title`, `slug`, `excerpt`, `summary`, `introduction`, `read_time`, `is_featured`, `sort_order`, `published_on`, `published_at`, `warning`, `insights` | same names | slug kept verbatim (validator failures reported); `(collection, slug)` clash refuses the row; `NULL` callouts → `""` |
| | `status`, `deleted_at` | `status`, `archived_at` | draft→DRAFT, published→PUBLISHED, archived/deleted→ARCHIVED (`archived_at` = `deleted_at` or `updated_at`), review states→REVIEW |
| | `cover_image_id` | `cover_image` | via `media_map` / the media import's legacy map; must be a live public asset |
| | `created_by_id`, `updated_by_id` | `created_by`, `updated_by` | via `user_map` (unmapped → NULL, reported) |
| | `created_at`, `updated_at` | same | preserved (`updatedAt` of the payload) |
| `content_entry_categories/_tags/_badges` | link rows | `blog_entry_category/_tag/_badge` | re-import removes links that vanished from the source |
| `content_entry_slug_history` | `slug`, `is_active`, `note`, `created_at` | `blog_entry_slug_history` (`is_active` → `active`) | an active alias colliding with a live slug/alias is imported inactive (reported) |
| `content_content_block` | `id`, `component`, `body`, `order` | `blog_content_block` (`delivery_id`, `component`, `data`, `position`); `kind` derived | unknown component kept verbatim as RICH_TEXT (reported) |
| `content_entry_image` | `group_key`, `position`, `media_asset_id`, `external_url` | `blog_entry_image` | both sources → the URL (what the CMS delivered) wins; no source → refused |
| `content_entry_attribute_value` | `slot_key`, `value` | `blog_entry_attribute_value` | coerced to the slot type (lenient); misfits kept verbatim (reported) |
| `content_seo` | `meta_title`, `meta_description`, `canonical_url`, `keywords` | `blog_entry_seo` (`meta_title` → `seo_title`, `keywords` new column) | `NULL` → `""` (delivered as `null`) |
| `media_asset` | — | — | imported by the media package; referenced through `core_legacy_map` (`CMS`/`media_asset`) or `media_map` |
| `siteconfig_settings` | `default_meta_description`, `default_og_image_id` | `company_profile` (F3/DV-11) | `seo.services.legacy_import.import_site_seo_defaults` writes them through `company.services.profile.update_profile`; no other SEO field exists there |

### Main backend (`GoldenApp`) → seo

| Source (`goldenray_metadata`, model `Metadata`) | Target (`seo_page_metadata`) | Rule |
|---|---|---|
| `page` | `page` | normalised (lower-case, no leading/trailing slash); invalid keys refused; a clash with another source row refused |
| `title`, `description` | same | |
| `keywords` (jsonb list) | `keywords` (varchar[]) | a string is split on commas; blanks dropped; de-duplicated |
| `imageUrl` | `og_image_url` | public payload key `imageUrl` (uploaded `og_image` wins) |
| `ogtype` | `og_type` | invalid → `website` (reported); public payload key `ogtype` |
| `id` | `core_legacy_map` only | the public contract carries no integer id |

## Parity evidence

* `blog/tests/fixtures/legacy_cms/tables.json` — every CMS blog table exported read-only from `legacy_blog_cms`
  (`blog/tests/fixtures/export_legacy_tables.py`, `READ ONLY` transaction, personal columns masked; author bylines kept
  because they are published).
* `blog/tests/fixtures/legacy_cms/golden/` — 47 responses captured from the running legacy CMS
  (`http://127.0.0.1:18009/api/articles?…`, `blog/tests/fixtures/capture_golden.py`, GET only): `populate=*` with
  `pageSize=100`, the default list, four `fields[]` variants, a `filters[slug][$eq]` lookup for every slug
  (published, draft, archived, unknown) and for the alias `net-metering-explained` (with and without `fields`, and
  `$eqi`), `$eqi`, `$in`, `$contains`, `$ne`, `$null` ×2, `$gt/$gte/$lt/$lte`, a date filter, `isFeatured`,
  `documentId`, ignored unknown filter/sort fields, nine sort variants and seven pagination variants.
* `blog/tests/test_delivery_parity.py` imports the fixture through `legacy_import.import_all` and asserts
  **byte-identical** bodies for all 47 captured requests — first from the database, then from the response cache —
  plus the same bytes on `content/articles/<slug>/` for the five published slugs and after a no-op re-import.
* Review additions: seven order-significant variants (reversed `sort[n]` keys, repeated plain `sort`, a repeated
  `filters[slug][$eq]` and `pagination[page]` in both orders; 54 seeded goldens in total) and
  `test_one_warm_cache_serves_every_captured_variant` (every captured request through **one** response cache, twice).
* `blog/tests/fixtures/legacy_cms_enriched/` — write-path parity for what the seed leaves empty: `enrich.sql` applied
  to a **private** restored copy of the CMS database (`legacy_blog_cms_rv_content_blog`, never the shared one) served
  by a private legacy server; 9 captured requests (`capture_golden.py --set enriched`) and the exported tables. Covers
  CDN media on a cover FK, on a `coverImg` group row (NULL width/height) and on a body image, attribute values written
  in slot order plus a legacy `warning` fallback key, a published entry without `read_time` (attribute fallback), a
  category without a slug and NULL author bio/role and SEO text. Media resolve through the media import's
  `core_legacy_map` rows. All 9 are byte-identical.
* `seo/tests/fixtures/legacy_backend/` — `goldenray_metadata` export and the legacy `GET /api/metadata/` response;
  `seo/tests/test_legacy_import.py` asserts that `seo/metadata/<page>/` returns every legacy item minus its `id`.

Approved differences from the legacy contract (DV-21): 400 instead of 500 for unparseable filter values; the extended
`$in` / `fields` forms; empty SEO/author text delivered as `null` where the CMS might have stored `""` (every row in
the captured data stores `NULL`); `Cache-Control` is `public, max-age=60` (was `…, stale-while-revalidate=600`) with an
`ETag`.

Known residual difference (not in the captured data): a CMS image row holding **both** a media asset and an external URL
in the `coverImg` group of an entry without a cover FK — the CMS built `coverImage` from the asset but `imgUrls` from
the URL; the platform keeps exactly one source per image row (the URL, reported `two_sources` by the importer), so that
`coverImage` becomes the URL with null dimensions.

## Adversarial review (content-blog reviewer)

Each defect was reproduced by a failing test first; the tests stay in the suite.

| # | Defect | Fix | Tests |
|---|---|---|---|
| R1 | The public response cache key sorted query parameters **and** repeated values, so order-significant delivery queries shared an entry: `sort[1]=title&sort[0]=isFeatured` was served the cached body of `sort[0]=isFeatured&sort[1]=title` (and `sort=a&sort=b` vs `sort=b&sort=a`, a repeated `filters[slug][$eq]` or `pagination[page]`) — confirmed against the live CMS | opt-in `ordered_query=True` on `cache_response`/`serve_cached` (key = the query as sent); the delivery views use it; the default key is unchanged | `blog/tests/test_delivery_parity.py::test_one_warm_cache_serves_every_captured_variant` (+7 captured goldens), `core/tests/test_cache_utils.py::TestOrderedQueryKey` |
| R2 | `attributes` was delivered alphabetically; the CMS delivers its rows in insertion order (slot order as the Studio sent them) — byte difference for any entry with two or more values | delivery prefetch orders attribute values by `id` (importer and replace-all writer insert in source/client order) | enriched parity set |
| R3 | A CMS category without a slug was delivered with a generated slug (CMS: `"slug": null`), contrary to PLAN §7.2 row 4 "slugs preserved" and §7.6 #3 | `blog_category.slug` nullable (migration `0002`); the importer keeps NULL; the staff API sets one on an edit that sends `slug` (blank → generated) | enriched parity set, `test_legacy_import.py::test_taxonomy_and_schema_violations`, `test_taxonomy_api.py::test_a_legacy_category_without_a_slug_is_served_and_editable` |
| R4 | The revalidation handler was unbounded: 100+ paths × a 3 s timeout against a hanging website exceeds the drainer's 120 s Celery hard limit (worker killed, event re-claimed until parked, the rest of the batch delayed) | first transport failure ends the batch; `BLOG_REVALIDATE_BUDGET_SECONDS` (20) caps the call; skipped paths logged | `test_revalidation.py::test_a_website_that_is_down_costs_one_request_per_event`, `::test_a_slow_website_is_bounded_by_the_time_budget`, `::test_the_default_budget_fits_the_celery_soft_time_limit` |
| R5 | Revalidation listed only the index and the current slug; the website renders the article itself under an active alias URL (`fetchArticleBySlug` reads `data[0]`, ignoring `meta.redirect`), so an unpublished/archived/deleted article, an edit or a renamed author/category stayed live under the old URL until the ISR window expired | entry events and `blog.content_changed` also list the entries' active alias paths (captured before a delete retires them) | `test_revalidation.py::test_changes_also_revalidate_the_entrys_old_urls` (unpublish, archive, delete, publish, edit, author rename) |
| R6 | The SEO overview's SQL status used `TRIM` (spaces only) while the record's own rule strips all whitespace: a 160-character description with a trailing newline was `ok` on the entry and `warning` in the overview (likewise titles at 60, empty-but-newline descriptions) | `StripWhitespace` (`REGEXP_REPLACE` on `[[:space:]]`) for both lengths | `seo/tests/test_overview_and_sitemap.py::TestOverview::test_sql_status_matches_the_python_rules` (4 new cases) |
| R7 | Alias retirement (entry delete, slug reclaim) was a bulk `UPDATE` without `version`/`updated_by`; `slug-history/<uid>/deactivate/` ignored `expected_version` | bulk retirement stamps `updated_by`, `updated_at`, `version + 1`; deactivate takes `expected_version` (409 `stale_version`) | `test_workflow_api.py::TestSlugHistory::test_deactivate_honours_expected_version`, `::test_bulk_retirement_is_stamped_like_any_versioned_write` |
| R8 | The fake revalidation backend was only wired by `blog/tests/conftest.py`; any other app's test emitting `website.revalidate_requested` with a configured profile would make a real HTTP call | `BLOG_REVALIDATE_BACKEND = "fake"` in `flarize/settings/test.py` | `seo/tests/test_metadata_api.py::TestStaff::test_an_edit_reaches_the_website_through_the_fake_client_only` |

## Hand-over notes

* **Legacy shim (`/legacy/…`, legacy package):** `/studio-api/api/<collection>` can be served with
  `blog.services.delivery.active_collection` + `query_entries(parse_query(params))` + `blog.serializers.delivery.page_body`
  (same bytes). `/api/metadata/` (list with integer ids) can be rebuilt from `PageMetadata` + `core_legacy_map` ids.
* **Other website apps** (pages, FAQs, careers): add `<app>/services/sitemap.py` (`sitemap_entries()`,
  `SITEMAP_CACHE_NAMESPACES`) and `<app>/services/seo_overview.py` (`SEO_OVERVIEW_KIND`, `seo_overview_rows()` via
  `seo.services.overview.overview_rows`) and emit `website.revalidate_requested` with the changed paths.
* **Importer order:** users → media (legacy map `CMS`/`media_asset`) → `blog.services.legacy_import.import_all(tables,
  user_map=…)` → `seo.services.legacy_import.import_page_metadata(rows)` / `import_site_seo_defaults(row)`;
  `import_all(..., dry_run=True)` reports without writing.
* **Frontend:** revalidation keeps today's `{secret, path}` body; to switch to the signature, verify
  `X-Flarize-Signature` = `sha256=HMAC(secret, "<X-Flarize-Timestamp>.<raw body>")`.
