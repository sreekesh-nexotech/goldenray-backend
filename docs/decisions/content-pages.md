# content-pages — maintained website pages (`sitepages`) and FAQs (`faqs`)

Work package *content-pages* builds PLAN §2.8 `sitepages_*` / `faqs_*`, the §3.3 public `pages` and `faqs` endpoints,
the §3.4 staff `pages/…`, `faqs/…`, `faq-categories/…` endpoints (+ `career-page/…`), the §3.5 revalidation events
and the §7.2 rows 8–9 importers, on top of F1–F3/F-FIX and the shared `seo.models.SeoFields` / `seo/schema.py`
(reused, not redefined). Legacy source: `goldenray-backend/backend/cms/{sitepages,faqs,seo}` and `CMS_BLUEPRINT.md`.
Deviations: DV-16 … DV-19 in `docs/DEVIATIONS.md`.

## What exists

| Area | Where | Notes |
|---|---|---|
| Models | `sitepages/models/page.py`, `faqs/models/faq.py` | `sitepages_page`, `sitepages_page_seo` (OneToOne + `SeoFields`), `sitepages_page_text_slot`, `sitepages_page_image_slot`, `faqs_category`, `faqs_faq` (+ `SeoFields`). All `BaseModel`; partial unique indexes on live rows (page slug, page route, `(page, key)` per slot kind, category name, category slug); CHECKs on every enum (status, text-slot kind, `schema_type`), slug/route/slot-key formats and `max_length > 0`; list-screen indexes `(status, sort_order)`, `(group, sort_order)`, `(page, section, sort_order)`, `(status, updated_at)`. |
| Page services | `sitepages/services/pages.py` | list/detail querysets (counts annotated, slots prefetched), `update_page` (sort order), `transition` (publish/unpublish/archive/restore), `verify`, `touch` (aggregate version), dashboard counters. |
| Page content | `sitepages/services/content.py` | `update_text_slot` (cap + kind rules), `update_image_slot` (public image or external URL), `current_seo` / `update_seo` (row created on the first edit), `public_image_problem`. |
| Delivery | `sitepages/services/delivery.py`, `faqs/services/delivery.py` | the legacy public payloads (`page_content`, `faq_list`) and the Studio previews. |
| Registry | `sitepages/services/registry.py`, `manage.py seed_pages` | the 27 website routes and the 3 career-page slots of the legacy `seed_pages`; additive, idempotent. |
| Sitemap | `sitepages/services/sitemap.py` | `sitemap_entries() -> [{"path", "lastmod"}]` (see decision 9). |
| FAQ services | `faqs/services/{faqs,categories}.py` | create/edit, `publish_errors`, workflow, verify, reorder, categories with the in-use guard, dashboard counters. |
| Importers | `sitepages/services/legacy_import.py`, `faqs/services/legacy_import.py` | PLAN §7.2 #8–9 (contract below). |
| Parity | `sitepages/tests/parity/`, `*/tests/fixtures/legacy_cms/`, `*/tests/test_parity.py` | golden payloads captured from the legacy CMS; see "Parity evidence". |

## Endpoints

| Surface | Path | Permission |
|---|---|---|
| staff | `GET pages/` (filters `status`, `group`, `template`, `is_protected`, `verified`; `search`; `ordering`), `GET pages/<uid>/`, `GET pages/<uid>/seo/`, `GET pages/<uid>/preview/` | `pages.view` |
| staff | `PATCH pages/<uid>/` (`sort_order`), `PATCH pages/<uid>/text-slots/<key>/` (`value`), `PATCH pages/<uid>/image-slots/<key>/` (`asset`, `external_url`, `alt`), `PATCH pages/<uid>/seo/` | `pages.edit` |
| staff | `POST pages/<uid>/publish/`, `…/unpublish/`, `…/archive/`, `…/restore/` | `pages.publish` |
| staff | `POST pages/<uid>/verify/` | `pages.verify` |
| staff | `career-page/` — the same list/detail/seo/preview/slots/publish/unpublish, pinned to slug `career` | `career_page.view` / `edit` / `publish` |
| staff | `GET faqs/` (filters `status`, `page_uid`, `route`, `section`, `category`, `updated_after`, `include_archived`, `verified`; `search`; `ordering`), `GET faqs/<uid>/`, `GET faqs/<uid>/preview/` | `faqs.view` |
| staff | `POST faqs/` | `faqs.create` |
| staff | `PATCH faqs/<uid>/`, `POST faqs/reorder/` (`page`, `section`, `order: [uid]`) | `faqs.edit` |
| staff | `POST faqs/<uid>/publish/`, `…/unpublish/` | `faqs.publish` |
| staff | `POST faqs/<uid>/archive/`, `…/restore/` | `faqs.archive` |
| staff | `POST faqs/<uid>/verify/` | `faqs.verify` |
| staff | `faq-categories/` list/detail · POST · PATCH · DELETE (soft, guarded) | `faqs.view` · `create` · `edit` · `archive` |
| public | `GET pages/<slug>/`, `GET pages/?route=<route>` | anonymous, `public_read`, cached (`sitepages`, `media`, `company`) |
| public | `GET faqs/?route=<route>[&section=][&category=<slug>]` (`page=` alias of `route`) | anonymous, `public_read`, cached (`faqs`, `sitepages`) |

Every PATCH/action takes `expected_version` (409 `stale_version`). Unrouted methods (create/delete of pages, DELETE of
FAQs, PUT) are denied by the default-deny permission (403) before method dispatch.

## Decisions not spelled out in the PLAN

1. **Pages are a registry, not content.** No create, no delete, no title/route/status PATCH: a page exists because the
   Next.js route exists (`seed_pages`, run after `migrate` on every release, or the importer). Maintainers change slot
   values, SEO, list order and the lifecycle. Seeding is a command, not a data migration, so test databases hold no
   seeded rows (CMS_BLUEPRINT §17 #17).
2. **`route` stays the key the website speaks; `slug` is the new public identifier.** Slug = the route's path
   (`/` → `home`, `/a/b` → `a-b`; `/career` → `career`, the career page per PLAN §7.2 #8). Both are unique among live
   pages. The public payload is served by slug and by route (the website's current query form).
3. **The page is an aggregate root.** A text/image-slot or SEO edit locks the page, writes the child row (own
   `version` for `expected_version`), then *touches* the page: version + 1, `updated_at` now, review stamp cleared,
   `sitepages.page_updated` when live. So `publish/`/`verify/` with the page's `expected_version` can never act on
   content the caller has not seen, and the page's `updated_at` is the sitemap `lastmod`.
4. **Reads never write.** The legacy `GET …/seo/` created the SEO row; here it returns unsaved defaults (`uid` null,
   version 1) and the first PATCH creates the row and edits it (version 2, the company-profile rule), so a concurrent
   first edit is detected. Until then the public `seo` is `null` (legacy semantics).
5. **Lifecycle tables** (pages: `pages.publish`; FAQs: `faqs.publish` for publish/unpublish, `faqs.archive` for
   archive/restore): publish DRAFT|ARCHIVED → PUBLISHED, unpublish PUBLISHED → DRAFT, archive DRAFT|PUBLISHED →
   ARCHIVED, restore ARCHIVED → DRAFT. Asking for the current state is an idempotent no-op; anything else is 409
   `invalid_transition`. FAQ `published_at` is sticky (first publication), `archived_at` follows archive/restore.
   FAQ publish is refused with 400 `faq_not_publishable` and the legacy reasons (`publish_errors`, shown on every
   read); a PUBLISHED FAQ cannot be edited into an incomplete state (same code) — unpublish it first.
6. **`verify`** (the registry's `pages.verify` / `faqs.verify`, never implemented by the legacy CMS) stamps
   `verified_by`/`verified_at` on the current content; every content change clears the stamp; archived records cannot
   be verified. It gates nothing (publishing does not require it); the Studio lists unverified content
   (`?verified=false`, dashboard `unverified`).
7. **Text-slot rules are enforced on every edit**: the cap (`This field holds up to N characters — you have M.`, the
   legacy message), and by kind — URL (http(s) or a site path), EMAIL, PHONE, single-line SHORT_TEXT. An empty value
   always passes (the page falls back to its shipped text). Image slots take only live **public IMAGE** assets with a
   CDN URL (or an `external_url`); every asset FK is registered with `media.usage`, so a used image cannot be deleted.
8. **Public payloads are the legacy contracts, byte for byte** (parity below): `{"data": {route, name, images, text,
   seo}}` and `{"data": [{id, question, answer, section, category, order}], "meta": {page, count, schema}}`, with one
   change: the FAQ `id` is the uid (integer ids never leave the service layer; the website uses it as a list key).
   Kept legacy rules: FAQs resolve by route whatever the page's status (the Next.js route renders regardless); only
   PUBLISHED pages serve page content; `section` present filters exactly — `section=` selects the unnamed section — on
   the public **and** the staff list (§17 #26); the WebPage/FAQPage JSON-LD comes from `seo/schema.py`; the site
   origin is the new `FRONTEND_BASE_URL` setting (the legacy setting's name and default) and the organisation name is
   the company profile's. A missing `route` is 400 (legacy: 404). The public FAQ list is capped at 200 rows.
9. **Sitemap convention**: `<app>.services.sitemap.sitemap_entries() -> [{"path": str, "lastmod": datetime}]`.
   `sitepages` lists PUBLISHED pages that are not `noindex`; `lastmod` = max(page `updated_at`, last change of a FAQ
   on it that has ever been published). FAQs are not addressable on their own, so `faqs` contributes no module.
10. **Revalidation events** (emit only; the SEO/blog package owns the handler). Every payload carries `paths` (site
    paths to revalidate):

    | Event | When | Payload |
    |---|---|---|
    | `sitepages.page_published` / `page_unpublished` / `page_archived` | status change into or out of PUBLISHED (archiving a draft emits nothing) | `page_uid`, `slug`, `route`, `status`, `previous_status`, `paths` |
    | `sitepages.page_updated` | slot or SEO edit of a PUBLISHED page | `page_uid`, `slug`, `route`, `status`, `paths` |
    | `faqs.faq_published` / `faq_unpublished` / `faq_archived` | status change into or out of PUBLISHED | `faq_uid`, `page_uid`, `status`, `previous_status`, `paths` |
    | `faqs.faq_updated` | edit of a PUBLISHED FAQ's question/answer/page/section/category/order | `faq_uid`, `page_uid`, `status`, `paths` (old and new page) |
    | `faqs.faqs_reordered` | a reorder moved a PUBLISHED FAQ | `page_uid`, `section`, `paths` |
    | `faqs.category_updated` | a category rename with published FAQs | `category_uid`, `paths` |

    Events are written in the service's transaction (dispatched after commit — CMS_BLUEPRINT §17 #2), one per change
    (#4; page/FAQ events carry a per-version `dedup_key`), and FAQ changes revalidate too (#23).
11. **Caches**: namespaces `sitepages` (every page/slot/SEO write, registry sync, import), `faqs` (every FAQ/category
    write, import); public pages also depend on `media` (alt text/URLs) and `company` (organisation name), public FAQs
    on `sitepages` (page name/route). Redis TTL 300 s, HTTP `max-age` 60 s.
12. **Categories** are soft-deleted; DELETE is 409 `category_in_use` while any live FAQ (archived included) uses it
    (legacy remediation text); names and slugs are unique among live categories (409 `category_name_taken` /
    `category_slug_taken`); a blank slug is derived from the name on create; list counts are annotated (§17 #25).
13. **Reorder** keeps the legacy semantics (uids outside the page/section are ignored and take no position; members
    left out follow in their previous order) and additionally refuses duplicate uids (400). Moving a FAQ to another
    page/section without a `sort_order` puts it at the end of the new list.
14. **Dashboard**: `pages` → `published`, `draft`, `unverified`; `faqs` → `published`, `draft`, `archived`.

## Legacy mapping (every legacy column has a home)

`sitepages_page` → `sitepages_page`

| Legacy | New | Import |
|---|---|---|
| `id` | `core_legacy_map` (`CMS`, `sitepages_page`) | natural-key match on `route` when unmapped (registry pages) |
| `name` | `title` | |
| `route` | `route` | must match `^/[^\s?#]*$`, else skipped (`invalid_route`) |
| — | `slug` | derived from the route (`slug_taken` if another page holds it) |
| `description`, `group`, `is_protected`, `sort_order` | same | |
| `status` (`draft`/`published`/`archived`) | `status` (upper case) | unknown → skipped (`unknown_status`) |
| `created_at`, `updated_at` | same | source timestamps preserved |
| `created_by_id`, `updated_by_id` | `created_by`, `updated_by` | through the user map; unmapped → empty (`unmapped_user`) |
| — | `template`, `verified_at`, `verified_by` | empty |

`sitepages_page_seo` → `sitepages_page_seo`: `seo_title`, `meta_description`, `canonical_url`, `schema_type` (unknown →
`none`, listed), `schema_extra`, `noindex` copied; `og_image_id` → `og_image` (media map, `unmapped_media`);
`page_id` → `page` (page map, else skipped `unmapped_page`); `updated_by_id`, `updated_at` → same (`created_at` =
`updated_at`); new `og_title`/`og_description` empty.

`sitepages_page_text_slot` → `sitepages_page_text_slot`: `key`, `label`, `guidance`, `value` (kept verbatim even when
over the new rules, listed `value_out_of_rules`), `max_length` copied; `kind` upper-cased (`short_text`,
`long_text`, `url`, `email`, `phone`; unknown → skipped); `order` → `sort_order`; `page_id`, `updated_by_id`,
`updated_at` as above. Natural key `(page, key)`.

`sitepages_page_image_slot` → `sitepages_page_image_slot`: `key`, `label`, `guidance` copied; `alt_text` → `alt`;
`asset_id` → `asset` (media map); `order` → `sort_order`; `page_id`, `updated_by_id`, `updated_at` as above; new
`external_url` empty. Natural key `(page, key)`.

`faqs_category` → `faqs_category`: `name`, `slug`, `description`, `is_active`, `sort_order`, `created_at`,
`updated_at` copied; natural key `slug`; a name/slug held by another live category → skipped (`category_taken`).

`faqs_faq` → `faqs_faq`: `question`, `answer`, `section`, `published_at`, `archived_at`, `seo_title`,
`meta_description`, `canonical_url`, `schema_type`, `schema_extra`, `noindex`, `created_at`, `updated_at` copied;
`display_order` → `sort_order`; `status` upper-cased; `page_id` (page map; unmapped → skipped), `category_id`
(category map; unmapped → empty), `og_image_id` (media map), `created_by_id`/`updated_by_id` (user map). A
PUBLISHED row the new publish rules would refuse is kept published and listed (`published_incomplete`).

Legacy computed fields: `effective_alt` → `PageImageSlot.effective_alt`; `seo_status`/`seo_issues` →
`SeoFields`; `publish_errors` → `faqs.services.faqs.publish_errors`; `faq_count`/`image_slot_count` → annotations;
`next_display_order` → `next_sort_order`.

Legacy endpoints: `/api/page-content?route=` → `GET /api/public/v1/pages/?route=` (and `pages/<slug>/`);
`/api/faqs?page=&section=` → `GET /api/public/v1/faqs/?route=` (`page=` accepted); `admin-api/pages/{id}/…` →
`pages/<uid>/…` with slots addressed by `key`; `admin-api/career-page/…` → `career-page/…`; `admin-api/faqs/…` →
`faqs/…` (DELETE → `archive/`); `faqs/reorder/` takes uids; `admin-api/faq-categories/` → `faq-categories/`.

## Legacy import contract

`<app>/services/legacy_import.py`: `import_pages`, `import_page_seo`, `import_text_slots`, `import_image_slots`,
`import_all` (sitepages); `import_categories`, `import_faqs`, `import_all` (faqs). Each takes plain row dicts (legacy
column names; timestamps as ISO strings or datetimes), returns `{"created", "updated", "skipped", "violations":
[{source_table, source_id, code, message}]}`, is idempotent through `core_legacy_map` (mapped → update when changed
else skip; unmapped → natural-key match → link, else create), preserves source timestamps/attribution, supports
`dry_run=True` (exact counts, rolled back), writes one audit row per call (`sitepages.legacy_import` /
`faqs.legacy_import`: counts + SHA-256 of the source rows) and bumps its cache namespace. It emits no outbox events
(PLAN §7.2: importing publishes nothing new). Order: users → media → pages → SEO/slots → categories → FAQs.

## Parity evidence

Captured with `sitepages/tests/parity/capture_legacy.py` (routes enumerated from the legacy database in a read-only
transaction; the legacy public FAQ endpoint takes the route in `page`):

| Dataset | Source | Exports | Golden |
|---|---|---|---|
| `uat` | shared legacy CMS `127.0.0.1:18009` / `legacy_blog_cms` (read only) | 27 pages, 2 text slots, 1 image slot, 0 SEO, 0 categories, 100 FAQs | 28 page-content queries (27 routes + unknown), 29 FAQ queries (14 routes, 14 `(route, section)` pairs, unknown) |
| `enriched` | private copy `flarize_wp_content_pages_legacy_cms` + `parity/enrich_legacy_cms.py`, private server on port 18137 | 27 pages (1 draft, 1 archived), 6 text slots, 3 image slots, 4 SEO rows, 3 media, 3 categories (1 inactive), 106 FAQs (drafts, archived, sections, ties, categories, HTML answers, a blank answer, FAQs on a draft page) | 28 page-content queries (25 × 200, 3 × 404), 35 FAQ queries |
| `enriched` after writes | `parity/apply_legacy_writes.py` ran 13 Studio operations through the legacy services/serializers (reorder with foreign + unknown ids, publish, refused publish, archive, unpublish, restore, text slot, refused over-cap text, image replace/reset, SEO edit creating WebPage schema, page publish/unpublish) | — | the same 28 + 35 queries re-captured |

Tests: `sitepages/tests/test_parity.py` and `faqs/tests/test_parity.py` import the exports through the importers and
assert **equal JSON** for every golden query — pages via `?route=` and via `<slug>/`, FAQs via `?route=` and `?page=`
(the only normalisation: legacy FAQ `id` → the uid recorded in `core_legacy_map` for that source row). The write
tests replay the same 13 operations through the new staff API (including the two refusals with the legacy messages)
and assert equality with the `*_after_writes` golden files. Zero differences.

## Hand-over notes

* **Deploy**: run `manage.py seed_pages` after `migrate` (idempotent). Set `FRONTEND_BASE_URL` (https origin of the
  website) in every environment.
* **Legacy shim** (`/legacy/api/page-content`, `/legacy/api/faqs`): call `sitepages.services.delivery.page_content(
  delivery.published_page(route=…))` and `faqs.services.delivery.faq_list(route, section=…)`; the legacy FAQ `id`
  was an integer — map uids back through `core_legacy_map` if the old frontend needs numbers (it only keys lists on it).
* **SEO/blog package**: consume the events of decision 10 for revalidation; aggregate `sitemap_entries()`; the SEO
  overview can read `PageSeo`/`Faq` through `SeoFields.seo_status()`.
* **migrations_tools** (`import_cms`): read the CMS tables and pass rows to the importers above after the user and
  media imports; the verification step can compare the public payloads exactly as the parity tests do.
