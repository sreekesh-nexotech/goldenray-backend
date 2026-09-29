-- Enrichment of the PRIVATE copy of legacy_goldenapp used by the calculators/EMI "enriched" corpora (never the shared
-- UAT database). Run once on a fresh restore:
--   createdb legacy_goldenapp_calculators_emi && pg_restore -d legacy_goldenapp_calculators_emi legacy_goldenapp.dump
--   psql -d legacy_goldenapp_calculators_emi -f calculators/tests/parity/enrich_private.sql
-- It reaches legacy paths the UAT rows never take: a missing final cost / interest rate / inverter price, fractional
-- and uneven sizes, more batteries and devices, and an EMI policy where kW, cost and loan bands, locked and floating
-- rates, priorities and specificity all compete.

-- website calculators ------------------------------------------------------------------------------------------------
UPDATE solar_installation_new SET final_cost = NULL WHERE id = 14;                       -- Commercial 24000: float(None) → 500
UPDATE solar_installation_new SET interest_rate = NULL WHERE id = 16;                    -- Commercial 40000: rate None → 0
UPDATE solar_installation_new SET interest_rate = 7.25, loan_available = '1,50,000-3,00,000' WHERE id = 3;
UPDATE solar_installation_new SET inverter_price = 61500.50, time_to_complete = '4-9' WHERE id = 4;
UPDATE solar_installation_new SET loan_available = 'on request' WHERE id = 5;             -- unparseable loan → 0
INSERT INTO solar_installation_new (bill_range, power_capacity, time_to_complete, total_cost, total_subsidy, area_required, loan_available, per_kw_rate, final_cost, interest_rate, type, inverter_price, created_at, updated_at)
VALUES (12000, 6.5, '3-8', 410000.00, 78000.00, 520, '2,00,000-4,00,000', 63077.00, 332000.00, 8.15, 'Residential', 58000.00, '2026-09-28 18:00:00+00', '2026-09-28 18:00:00+00'),
       (2500, 1.5, '2-4', 120000.00, 45000.00, 120, '75,000', 80000.00, 75000.00, 5.50, 'Residential', NULL, '2026-09-28 18:00:01+00', '2026-09-28 18:00:01+00');
INSERT INTO solar_installations (power_capacity, time_to_complete, total_cost, total_subsidy, area_required, created_at, updated_at)
VALUES (1, 2, 90000.00, 30000.00, 80, '2026-09-28 18:00:02+00', '2026-09-28 18:00:02+00'),
       (2, 3, 150000.00, 60000.00, 160, '2026-09-28 18:00:03+00', '2026-09-28 18:00:03+00'),
       (2.5, 3, 185000.00, 70000.00, 200, '2026-09-28 18:00:04+00', '2026-09-28 18:00:04+00'),
       (20, 12, 1300000.00, 78000.00, 1600, '2026-09-28 18:00:05+00', '2026-09-28 18:00:05+00');
INSERT INTO batteries (battery_capacity, backup_hour, battery_price, created_at, updated_at)
VALUES (2.56, 2.50, 72000.00, '2026-09-28 18:00:06+00', '2026-09-28 18:00:06+00'),
       (10.24, 12.00, 265000.00, '2026-09-28 18:00:07+00', '2026-09-28 18:00:07+00'),
       (7.68, 9.00, 199999.99, '2026-09-28 18:00:08+00', '2026-09-28 18:00:08+00');
INSERT INTO device_types (name, show_in_ui, url, watts, k_value, created_at, updated_at)
VALUES ('Water Pump', true, NULL, 750, 0.5, '2026-09-28 18:00:09+00', '2026-09-28 18:00:09+00'),
       ('Router', true, NULL, NULL, NULL, '2026-09-28 18:00:10+00', '2026-09-28 18:00:10+00'),
       ('Induction Cooktop', false, NULL, 2000, 0, '2026-09-28 18:00:11+00', '2026-09-28 18:00:11+00');
INSERT INTO ev_cars (model, battery_capacity, claimed_range, adjusted_real_world_range, ex_showroom_price, energy_consumption, k_value, created_at, updated_at)
VALUES ('Prototype EV', 40.0, 400, 300, 1500000, NULL, 1.0, '2026-09-28 18:00:12+00', '2026-09-28 18:00:12+00'),
       ('Fleet Van EV', 60.0, 350, 260, 2400000, 0.231, 0.8, '2026-09-28 18:00:13+00', '2026-09-28 18:00:13+00');
UPDATE kseb_tariffs SET rate = 6.95 WHERE id = 1;

-- EMI calculator ---------------------------------------------------------------------------------------------------------
UPDATE emi_interest_rate_rule SET is_active = true WHERE id IN (1, 2, 3, 4);            -- kW and system-cost bands compete
UPDATE emi_interest_rate_rule SET priority = 20, rate = 9.00, min_rate = 7.50, is_locked = false WHERE id = 6;
INSERT INTO emi_interest_rate_rule (label, min_kw, max_kw, min_cost, max_cost, min_loan, max_loan, rate, min_rate, is_locked, priority, is_active, created_at, updated_at)
VALUES ('8–10 kW, loan ≤ ₹4.5L (floating)', 8.00, 10.00, NULL, NULL, NULL, 450000.00, 7.40, 6.90, false, 30, true, '2026-09-28 18:01:00+00', '2026-09-28 18:01:00+00'),
       ('Any size, cost ≥ ₹7L', NULL, NULL, 700000.00, NULL, NULL, NULL, 8.60, 8.60, true, 25, true, '2026-09-28 18:01:01+00', '2026-09-28 18:01:01+00'),
       ('Inactive promo', NULL, NULL, NULL, NULL, NULL, 300000.00, 4.99, 4.99, true, 99, false, '2026-09-28 18:01:02+00', '2026-09-28 18:01:02+00');
INSERT INTO emi_subsidy_rule (label, min_kw, max_kw, amount, priority, is_active, created_at, updated_at)
VALUES ('State top-up 5–8 kW', 5.00, 8.00, 85000.00, 20, true, '2026-09-28 18:01:03+00', '2026-09-28 18:01:03+00'),
       ('Retired scheme', NULL, NULL, 99000.00, 50, false, '2026-09-28 18:01:04+00', '2026-09-28 18:01:04+00');
INSERT INTO emi_system_size (label, capacity_kw, price_per_kw, price_min, price_max, monthly_bill_reference, sort_order, is_active, created_at, updated_at)
VALUES ('6.5kW', 6.50, 64999.99, NULL, NULL, 12500.00, 5, true, '2026-09-28 18:01:05+00', '2026-09-28 18:01:05+00'),
       ('4kW', 4.00, 70000.00, 250000.00, NULL, 8000.00, 2, true, '2026-09-28 18:01:06+00', '2026-09-28 18:01:06+00'),
       ('12kW (hidden)', 12.00, 58000.00, 600000.00, 800000.00, 25000.00, 6, false, '2026-09-28 18:01:07+00', '2026-09-28 18:01:07+00');
INSERT INTO emi_bank (name, abbr, slug, logo_bg, interest_rate, min_loan, max_loan, upfront_requirement, eligibility, cibil_required, processing_fee_percent, processing_fee_note, approval_min_days, approval_max_days, max_tenure_years, features, best_for, is_recommended, sort_order, is_active, created_at, updated_at)
VALUES ('Kerala Gramin Bank', 'KGB', 'kgb', '#0F766E', 7.90, 50000.00, 400000.00, '15% margin', 'Rural customers', 600, 0.35, '', 7, 14, 8, '["Rural branches", "Local language support"]', 'Rural homes', false, 3, true, '2026-09-28 18:01:08+00', '2026-09-28 18:01:08+00'),
       ('Old Partner Bank', 'OPB', 'old-partner', '#111827', 9.99, 0.00, 0.00, '', '', 0, 0.00, '', 0, 0, 0, '[]', '', false, 9, false, '2026-09-28 18:01:09+00', '2026-09-28 18:01:09+00');
UPDATE emi_calculator_settings SET down_payment_min_percent = 15.00, tenure_default_years = 7, daily_saving_divisor = 31, default_interest_rate = 10.25, down_payment_quick_adds = '[2500, 7500.5, 15000]' WHERE id = 1;
