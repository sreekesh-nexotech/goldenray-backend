-- Enriched legacy CMS fixture (blog/tests/fixtures/legacy_cms_enriched/): exercises delivery paths the UAT seed leaves
-- empty — CDN media on a cover FK, on a coverImg group row and on a body image; attribute values written in slot order
-- (the CMS delivers `attributes` in insertion order) incl. a legacy fallback key; a published entry without read_time;
-- a category without a slug; NULL author bio/role and NULL SEO text.
--
-- NEVER run against the shared read-only legacy database. Reproduce on a private copy:
--   createdb legacy_blog_cms_rv_content_blog
--   pg_restore -d legacy_blog_cms_rv_content_blog --no-owner /home/user/platform-reference/uat/legacy_blog_cms.dump
--   psql -d legacy_blog_cms_rv_content_blog -v ON_ERROR_STOP=1 -f blog/tests/fixtures/legacy_cms_enriched/enrich.sql
--   (source /home/user/legacy-env.sh; DB_NAME=legacy_blog_cms_rv_content_blog manage.py runserver 127.0.0.1:18147)
--   python blog/tests/fixtures/capture_golden.py --set enriched --base http://127.0.0.1:18147
--   python blog/tests/fixtures/export_legacy_tables.py --database legacy_blog_cms_rv_content_blog \
--       --out blog/tests/fixtures/legacy_cms_enriched/tables.json --table … (the tables of legacy_cms/tables.json)
BEGIN;
INSERT INTO media_asset (id, file, mime, size, width, height, alternative_text, caption, created_at, updated_at, cdn_url, collection_id, storage_path) VALUES
 (1, 'uploads/cover-1.jpg', 'image/jpeg', 1000, 1200, 630, 'Rooftop array', '', now(), now(), 'https://golden-ray.b-cdn.net/blog/cover-1.jpg', NULL, 'blog/cover-1.jpg'),
 (2, 'uploads/body-2.webp', 'image/webp', 1000, 800, 600, '', '', now(), now(), 'https://golden-ray.b-cdn.net/blog/body-2.webp', NULL, 'blog/body-2.webp'),
 (3, 'uploads/cover-3.jpg', 'image/jpeg', 1000, NULL, NULL, 'Cover group', '', now(), now(), 'https://golden-ray.b-cdn.net/blog/cover-3.jpg', NULL, 'blog/cover-3.jpg');
UPDATE content_entry SET cover_image_id = 1 WHERE id = 3;
INSERT INTO catalog_template_attribute_slot (id, key, label, type, options, required, "order", template_id) VALUES
 (2, 'readTime', 'Read time', 'number', '{}', false, 2, 1),
 (3, 'showCta', 'Show CTA', 'boolean', '{}', false, 3, 1),
 (4, 'audience', 'Audience', 'text', '{}', false, 4, 1);
-- values inserted in slot order (not alphabetical): the CMS delivers them in insertion order
INSERT INTO content_entry_attribute_value (id, entry_id, slot_key, value) VALUES
 (1, 1, 'difficulty', '"Beginner"'),
 (2, 1, 'showCta', 'true'),
 (3, 1, 'audience', '"Homeowners"'),
 (4, 2, 'readTime', '11'),
 (5, 5, 'warning', '"From the slot"'),
 (6, 5, 'difficulty', '"Advanced"');
UPDATE content_entry SET read_time = NULL WHERE id = 2;
INSERT INTO catalog_category (id, name, slug) VALUES (4, 'Aardvark topics', NULL);
INSERT INTO content_entry_categories (entry_id, category_id) VALUES (4, 4);
UPDATE catalog_author SET bio = NULL, role = NULL WHERE id = 2;
UPDATE content_seo SET canonical_url = NULL, keywords = NULL WHERE entry_id = 5;
-- a media-backed body image and a media-backed cover group image on an entry without a cover FK
UPDATE content_entry_image SET media_asset_id = 2, external_url = '' WHERE id = 10;
UPDATE content_entry_image SET media_asset_id = 3, external_url = '' WHERE id = 12;
COMMIT;
