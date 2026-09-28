# Deviations from PLAN.md and the standard

| # | Where | Deviation | Reason |
|---|---|---|---|
| DV-1 | §2.1 accounts_user.employee_id | The link lives on `hr_employee.user` (OneToOne) instead of `accounts_user.employee_id` | accounts is platform core and must not import hr (dependency direction) |
| DV-2 | §2.6 quotations_quotation.agreement_id / site_inspection_id | Links are held on the dependent side (`agreements_agreement.quotation_version`, `site_inspections_inspection.quotation_version`) | avoids circular app dependencies; reads resolve through reverse relations |
| DV-3 | §2.6 agreements_agreement.site_inspection_id | Stored as `source_type`/`source_uid`; the FK lives on `site_inspections_additional_work_item.agreement` | site_inspections depends on agreements, not the reverse |
| DV-4 | §6.2 `/bom/**` "no shim" | `/legacy/bom/api/calculate/` is shimmed with byte-parity to the old `BomCalculator` | the website quotation page (`frontend/src/services/bomService.ts`) calls it directly |
| DV-5 | §3.1 legacy "same paths" | Django serves old contracts only under `/legacy/…`; nginx rewrites the old public URLs to `/legacy/…` | keeps the application URL space strictly versioned |
| DV-6 | §2.3 inventory tables | Implemented in a dedicated `inventory` app | the plan lists tables but no owning app |
