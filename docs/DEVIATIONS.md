# Deviations from PLAN.md and the standard

| # | Where | Deviation | Reason |
|---|---|---|---|
| DV-1 | §2.1 accounts_user.employee_id | The link lives on `hr_employee.user` (OneToOne) instead of `accounts_user.employee_id` | accounts is platform core and must not import hr (dependency direction) |
| DV-2 | §2.6 quotations_quotation.agreement_id / site_inspection_id | Links are held on the dependent side (`agreements_agreement.quotation_version`, `site_inspections_inspection.quotation_version`) | avoids circular app dependencies; reads resolve through reverse relations |
| DV-3 | §2.6 agreements_agreement.site_inspection_id | Stored as `source_type`/`source_uid`; the FK lives on `site_inspections_additional_work_item.agreement` | site_inspections depends on agreements, not the reverse |
| DV-4 | §6.2 `/bom/**` "no shim" | `/legacy/bom/api/calculate/` is shimmed with byte-parity to the old `BomCalculator` | the website quotation page (`frontend/src/services/bomService.ts`) calls it directly |
| DV-5 | §3.1 legacy "same paths" | Django serves old contracts only under `/legacy/…`; nginx rewrites the old public URLs to `/legacy/…` | keeps the application URL space strictly versioned |
| DV-6 | §2.3 inventory tables | Implemented in a dedicated `inventory` app | the plan lists tables but no owning app |
| DV-7 | §2.1 core_outbox_event | Three extra columns: `dedup_key` (partial unique, idempotent emit), `parked_at` (poison-pill rows after 5 attempts, distinct from `processed_at`), `delivered` (handlers that already succeeded, never re-run on retry) | the standard's outbox rules (§7.2: idempotency by unique constraint, poison-pill guard, no double-fire) need state the PLAN table does not carry |
| DV-8 | §3.1 throttles | Extra scope `token_refresh` (300/15min per client IP) for `auth/refresh/` and `auth/logout/` instead of `login` (10/15min) | every active Studio user refreshes every 15 min, often from one office NAT or the BFF host; sharing the login budget would sign whole offices out |
| DV-9 | §2.1 audit_log.actor_id | `actor_id` is an indexed bigint without a database FK constraint (the Django relation uses `db_constraint=False`, `DO_NOTHING`) | an `ON DELETE SET NULL` action would rewrite rows of an append-only ledger (and blocks `TRUNCATE accounts_user`); users are only soft-deleted, so the reference never dangles |
