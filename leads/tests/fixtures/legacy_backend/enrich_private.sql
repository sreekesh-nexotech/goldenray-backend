-- Enrichment of the PRIVATE copy of legacy_goldenapp used by leads/tests/fixtures/capture_legacy.py (never the shared
-- UAT database): installations in the capture year, other statuses, multi-district, out-of-table and foreign pincodes.
INSERT INTO customer_installations (customer_name, phone_number, pincode, address, system_size, installation_date, status, created_at, updated_at) VALUES
('Enriched One', '+919800000101', '688008', 'Enriched address 1', 4.5, '2026-03-10', 'completed', now(), now()),
('Enriched Two', '+919800000102', '682016', 'Enriched address 2', 3.0, '2026-06-01', 'completed', now(), now()),
('Enriched Three', '+919800000103', '688008', 'Enriched address 3', 5.0, '2026-07-15', 'in_progress', now(), now()),
('Enriched Four', '+919800000104', '695001', 'Enriched address 4', 6.0, '2026-08-20', 'planned', now(), now()),
('Enriched Five', '+919800000105', '689122', 'Enriched address 5', 3.0, '2026-02-02', 'completed', now(), now()),
('Enriched Six', '+919800000106', '686102', 'Enriched address 6', 2.0, '2025-11-11', 'completed', now(), now()),
('Enriched Seven', '+919800000107', '682999', 'Enriched address 7', 7.25, '2026-01-05', 'completed', now(), now()),
('Enriched Eight', '+919800000108', '110001', 'Enriched address 8', 10.0, '2026-04-04', 'completed', now(), now());
