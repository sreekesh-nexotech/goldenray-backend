# Business defaults (assumed 2026-09-29, pending owner review)

The owner asked us to assume decisions from the current state of the website repo and proceed; any of these can be changed later.
Rule of thumb: keep today's live behaviour, unless it leaks cost/margin to the public.

Copied from `/home/user/platform-reference/business-defaults.md` at the wave-4a integration; B-13 and B-14 were added
then from the quotations package's open questions; B-15 and B-16 at the wave-4d integration from the migration-ops
open issues. B-8 is implemented (`attendance/tests/test_process_scope.py`).

| # | Question | Decision | Basis |
|---|----------|----------|-------|
| B-1 | Public BOM quote exposes `cost_breakdown` and `totals` (internal cost and margin) | **Strip `cost_breakdown` and `totals` from every PUBLIC response** (POST /api/public/v1/bom/quote/ and the /legacy/bom/api/calculate/ shim). Keep `bom_lines`, `pricing`, `meta`, `available_offers` byte-identical. The service `bom.services.website_quote.quote()` keeps returning the full legacy body (byte-parity test unchanged); the public view/shim filters. Staff callers with `pricing_internal` may get the full body. | frontend `src/services/bomService.ts` reads only `bom_lines[].name/qty/unit` and `pricing.*`; nothing reads cost_breakdown/totals |
| B-2 | Flarize values win over legacy on import (D-2), e.g. SS Terminal Strip 4P 140 -> 150, transport per km from Flarize config | **Keep D-2 (Flarize wins)**; the import report lists every changed value for sales | PLAN D-2 |
| B-3 | Pack config market rates vs catalog.json (228000 -> 229000) | **Approved pack config wins** | it is the approved, newer source |
| B-4 | bt1 battery PBC-H-002 blocks every hybrid pack | **Keep blocking; no automatic waiver.** Hybrid packs publish once engineering fixes bt1 data or acknowledges the finding | engineering safety rule |
| B-5 | Studio inverters stored as 1P/3P don't match 1P-HYB/3P-HYB slot filters | **Keep legacy matching** (byte parity); report affected components in the import report | legacy behaviour |
| B-6 | EMI price source | **MANUAL** (legacy tiles 3/5/8/10 kW) until 8/10 kW market rates exist | live website behaviour |
| B-7 | Attendance v3 -> v4 status changes (A1-A6) | **Adopt v4** as planned; the per-employee-month diff report goes to HR after cut-over import | PLAN A-rules |
| B-8 | attendance process/recalculate record scope | **Apply the caller's employee scope** (fail closed); manage-only as now | least privilege |
| B-9 | Recompute after transfer uses current office context | **Keep eSSL behaviour** | legacy behaviour |
| B-10 | Reversed procurement batch stock | **Manual RETURN/ADJUST** (no auto-reversal) | safe default |
| B-11 | packs.view sees installation/structure-material rates | **Keep as Flarize** (staff only) | legacy behaviour |
| B-12 | EMI import skips banks with malformed slug/logo colour | **Keep skipping, list them in the import report** | no such rows in data |
| B-13 | D-4 applied to the printed "You Pay" (offer and approved discounts subtracted), but the payload's savings/payback and EMI figures are computed on the price before those reductions | **Keep savings/payback/EMI on the pre-discount price** (Flarize parity) — only the printed price follows D-4 (DV-115); revisit with the owner | Flarize computes them before the offer; changing it breaks the offer parity cases (quotations open issue) |
| B-14 | An explicit `selections.offer_code` applies any ACTIVE offer without checking its system/tier/size or date window | **Keep: an explicit offer code may apply any ACTIVE offer** (Flarize parity); Sales is trusted with explicit codes | Flarize's explicit `offerId` behaves this way (quotations open issue) |
| B-15 | The second `import_flarize` publishes PriceRelease #2 / PackRelease #2 when `import_pa` added the KSEB statutory fees in between (migration-ops open issue) | **At cutover run `import_pa --only pa.kseb_fees` before `import_flarize`**, so PriceRelease #1 already carries the KSEB fees; the full `import_pa` follows `import_flarize` as before (runbook §7 "Data import order") | one release with the complete pricing masters; nothing is re-published at cutover |
| B-16 | 5 Flarize owner/user ids named in customers/workspace/quotation state are missing from `users.json` | **Leave them unmapped** (owners/users empty, `unmapped_owner` / `unmapped_user`) and **list them in the import report**; accounts are created by staff afterwards if the business wants them | no source record to build an account from; the import report makes them visible |
