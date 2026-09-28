# engines-ops — attendance, inspection checks and inspection readiness engines

Work package engines-ops ports the three operations engines of PLAN §1.5 (`engines.attendance`,
`engines.inspection_checks`, `engines.inspection_readiness`) as pure Python over plain dataclasses, with the v4
attendance changes A1–A12 (PLAN §2.9) and the Site Inspection V2 fixes (Plan 2 §3.2, spec §C/§J). No Django, no app
import (import-linter contract `engines-pure`, plus an AST test). Deviations: DV-17, DV-18.

## What exists

| Module | Public API | Tests |
|---|---|---|
| `engines/attendance.py` | data: `Shift` (PLAN `hr_shift` defaults), `Punch`, `RawPunch`, `DeviceLink`, `Employee`, `Holiday`, `Leave`, `AttendanceRule`, `DayContext`, `DayRules`, `DayResult`, `CalendarDay`; day: `compute_day`, `debounce`, `deduct_break`, `attribute_work_date`, `overnight_cutoff`, `shift_boundaries`, `is_working_day`, `effective_shift`, `half_day_deadline`, `describe_half_day_deadline`, `rules_for`, `rules_from_payload`, `validate_rules_payload`; window: `recompute`, `build_identity_map`, `unmapped_pins`, `affected_work_dates`, `HolidayCalendar`, `LeaveBook`, `day_context`, `context_lookup`; calendar: `calendar_fill`, `summarise`, `attendance_rate`, `combine_summaries`, `totals_by_date`, `month_bounds`, `hm`; clocks: `zone`, `is_valid_timezone`, `today_local`, `local_wall_clock`, `punch_at_from_device_time`, `clock_offset_seconds`, `finalise_target`, `punch_dedup_key`; corrections: `CORRECTABLE_FIELDS`, `apply_correction`; `PROCESSING_VERSION = "v4"`, `Status`, `STATUS_CODE`, `STATUS_LABEL`, `PRESENT_DAY_STATUSES`, `MISSING_OUT_LABEL` | `test_attendance_engine.py`, `test_attendance_recompute.py`, `test_attendance_calendar.py`, `test_attendance_parity.py` |
| `engines/inspection_checks.py` | `CHECKS_VERSION = "v1"`, `DEFINITIONS`, `checklist(type, version)`, `required_equipment_types`, `validate_results`, `normalise_results`, `compute_status`, `is_complete`, `is_critical`, `is_resolved`, `evidence_required`, `result_counts`, `legacy_status`; enums `EquipmentType`, `SystemType`, `CheckResult`, `AssessmentStatus`, `ReviewStatus` | `test_inspection_checks.py`, `test_inspection_parity.py` |
| `engines/inspection_readiness.py` | `InspectionState` (+ `Annotation`, `Approval`, `EquipmentAssessment`, `AdditionalWork`, `EngineeringReview`), `evaluate`, `field_completion`, `Readiness`/`Blocker` (`codes`, `as_dict()`), `Code`, `STEPS`, `STEP_OF`, `BLOCKER_TEXT`, `FIELD_COMPLETION_CODES`, `build_location_snapshot`, `location_snapshot_matches`, `engineering_review_pending`, `has_value`, `is_positive`, `normalise_suitability`, `normalise_availability`, `state_fields` | `test_inspection_readiness.py`, `test_inspection_parity.py` |
| `engines/tests/attendance_cases.py`, `inspection_cases.py` | the case tables shared by the tests and the parity capture scripts | — |
| `engines/tests/golden/essl_v3_attendance.json`, `si_legacy.json` | outputs of the real legacy engines for every case (parity evidence below) | — |
| `scripts/parity/capture_essl_v3.py`, `capture_si_legacy.py`, `si_legacy_runner.mjs` | re-capture the golden files from the legacy sources (not CI: the sources live outside the repo) | — |

Coverage of `engines/`: 100 % statements and branches (593 tests).

## Attendance v4 — where each A-row lives

| # | Engine part | Tests |
|---|---|---|
| A1 | `build_identity_map` keyed `(device, pin)`, no `employee_code` fallback, conflicting link = error; `recompute` resolves through it; `unmapped_pins` per device | `test_attendance_recompute.py::test_the_same_pin_on_another_device_is_another_person`, `…unmapped_pin_is_reported_per_device…`, `…no_employee_code_fallback`, `…cannot_belong_to_two_employees` |
| A2 | `deduct_break`: `min(break, max(0, gross − half_day_minutes))` | `test_break_deduction_is_continuous_and_monotonic` (5 shift variants, 0–1199 min), `test_break_boundaries_of_the_default_shift`, parity `test_break_deduction_differs_from_v3_exactly_on_the_discontinuity` (v3 and v4 differ exactly on gross 61–299) |
| A3 | `debounce` (within `debounce_minutes` of the previous *accepted* punch; `0` disables; first punch always kept); ignored ids in `DayResult.ignored_raw_ids`, `punch_count` counts accepted punches | `test_debounce_*`, cases `v4.a3_*`, `proc.duplicate_punches`, `multi.same_second_two_devices` |
| A4 | `compute_day`: punches on a holiday/weekly off → HOLIDAY/WEEKLY_OFF, `worked_on_off_day`, overtime = working minutes (0 if the shift disables overtime), no lateness/early exit/deadline; full-day leave with punches → status from punches + `leave_conflict`; half-day leave unchanged | cases `v4.a4_*`, `half.weekly_off_no_deadline`, `test_worked_holiday_is_overtime_and_never_late_or_half`, `test_full_day_leave_with_punches_is_flagged_not_hidden` |
| A5 | `half_day_deadline` = start + `shift.half_day_after_minutes` (30); rule minutes replace the allowance, rule clock time states the deadline; `rules_for` global < office < shift by `effective_from` | `DEADLINES` table (15, 5 changed vs v3), cases `half.*`, `cutoff.*`, `v4.a5_*`, `test_rules_scope_and_effective_dates` |
| A6 | `attribute_work_date` / `overnight_cutoff`: up to `end + overnight_buffer_minutes` (180) inclusive → previous date, capped below the shift start | `ATTRIBUTIONS` table (10, 3 changed), `test_overnight_punches_in_the_buffer_belong_to_the_previous_work_date` |
| A7 | `recompute` skips every date ≥ today in the employee's office timezone (`skipped_future`); `calendar_fill` leaves today/future blank (`Fill.PENDING`); `finalise_target(now, tz)` = yesterday on the office clock; weekly offs are written like every other finalised day | `test_no_row_is_written_for_today_or_later`, `test_the_first_push_of_the_day_stores_no_absence`, `test_today_follows_each_employees_office_timezone`, `test_future_days_are_blank_not_absent` |
| A8 | `affected_work_dates(instants, shift, timezone)` — the dates an ingestion batch can change (the event + debounced Celery recompute belong to the attendance app) | `test_affected_work_dates` |
| A9 | one `calendar_fill` (a missing past day is decided by the same rule as `compute_day` without punches), `summarise`, `attendance_rate` = `(present + late + 0.5 × half) / (present + late + half + absent)` as a fraction, `combine_summaries` pools counts for offices; codes `P LT A HD WO H L` | `test_attendance_calendar.py` |
| A10 | `Punch.punch_at` must be aware; the day is read on the office wall clock (`timezone=`); elapsed time from UTC instants (DST-safe); `zone()` validates IANA names; `punch_at_from_device_time` (no clock correction), `clock_offset_seconds` measured only; every "today" from an explicit `now` | `test_punches_are_read_on_the_office_wall_clock`, `test_elapsed_time_is_real_across_a_dst_change`, `test_timezone_names_are_validated`, `test_today_is_the_office_date`, `test_work_dates_follow_the_office_wall_clock` |
| A11 | `punch_dedup_key(serial, pin, device_time, status, punch)` = sha256 of `SERIAL|pin|iso device_time|status|punch` (serial stripped + upper-cased) | `test_dedup_key_is_a_content_hash_across_transports` |
| A12 | `recompute(corrected=…)` never rewrites a corrected day (`skipped_corrected`); `apply_correction` validates a correction | `test_a_corrected_day_is_never_overwritten`, `test_apply_correction` |

## Decisions not spelled out in the PLAN

1. **Pure inputs, explicit clocks.** The engine never reads the system clock or the database: `recompute` takes the
   employees (with the *effective* shift already resolved via `effective_shift`), raw punches, `(device, pin)` links,
   holidays, leave, rules, the corrected `(employee, date)` pairs and `now`. The caller fetches punches from the day
   before `date_from` to the day after `date_to` (overnight edges), as v3 did.
2. **Wall clock vs elapsed time.** Schedule comparisons (lateness, early exit, deadlines, work date) use the office wall
   clock; spans (gross minutes, debounce) use UTC instants. `first_in`/`last_out` are returned as naive office-local
   datetimes because `attendance_day.first_in/last_out` are `timestamp` (PLAN §2.9).
3. **No shift = no timing** (v3 C2): no lateness, early exit, break or overtime; full day 480; Sunday off. v3's 10:00
   wall-clock fallback deadline is gone with A5 — without a shift only a rule's clock time gives a deadline. The
   debounce still applies (2 minutes): double scans are a data problem, not a schedule rule.
4. **Rule precedence for the deadline** (A5): the deadline is one setting stated two ways, so the most specific/latest
   rule that states it wins whichever way it states it (v3 let an office clock time beat a shift rule in minutes;
   case `v4.a5_specific_rule_in_minutes_beats_office_clock`). Within one rule the clock time wins (v3). Malformed
   values are ignored when merging (v3); `validate_rules_payload` gives field errors for the `attendance-rules/` API.
5. **Status with punches on a working day** stays v3's: half-day leave → HALF_DAY; arrival after the deadline short of
   a full day (credited = working + grace credit; `half_day_under_minutes` narrows it) → HALF_DAY; else LATE/PRESENT.
   The v3 `LATE only on a working day` branch is subsumed by A4 (off days never reach it).
6. **`punch_count`** counts accepted punches; `source_raw_ids` + `ignored_raw_ids` together are every raw row of the
   day. `first_device` is the device of the first accepted punch (`attendance_day.first_device_id`).
7. **Leave precedence**: when a full-day and a half-day approved leave cover the same date, the full-day one counts;
   only APPROVED leave counts (`LeaveBook`, status compared case-insensitively). A holiday on the posting office is
   named before an all-offices one.
8. **Calendar blanks**: before `joined_on`/after `left_on` → `NOT_EMPLOYED` (checked first — v3 showed a holiday or
   leave before the join date); today and later → `PENDING`; both have status `""` and code `""`. `CalendarDay.as_dict()`
   is the display shape (clock strings, `Missing OUT`, `H:MM`). Stored rows may be ORM objects (attribute access only).
9. **Rates are fractions** (CLAUDE.md), `Decimal` quantised to 4 places, `None` when nothing was expected. A worked
   holiday/weekly off is not a present day; it is counted in `worked_off_days` and its minutes in overtime.
10. **Checklist versions.** `DEFINITIONS["v1"]` is the legacy list word for word (verified against the legacy module).
    A future wording change adds `"v2"`; stored assessments keep scoring against their pinned `checks_version`.
11. **Status never trusted.** Readiness recomputes each assessment's status from its answers and pinned version; a
    stored status is informational. Unknown check ids or result values are errors (legacy scored them CONDITIONAL).
12. **Critical assessments.** FAIL / REQUIRES_REVIEW is one blocker (`…_NEEDS_RESOLUTION`) until the review status is
    RESOLVED or WAIVED; a waiver never completes an unanswered checklist. Field completion requires every required
    checklist answered in full (FAIL allowed — the engineering review decides).
13. **Engineering review pending** = a review awaits a decision, or the latest decided review asked for changes, or the
    site is marked complex and no review found it routine/resolved it.
14. **Location snapshot.** `build_location_snapshot` stores, per annotation type, the photo uid, the `{x, y, w, h}`
    geometry (6 dp, `width`/`height` accepted as aliases), the geometry space and the measurements (3 dp) as strings;
    `location_snapshot_matches` compares canonical forms, so a JSON round trip or re-saving the same rectangle is not a
    change, while a new photo, a moved/resized rectangle, new measurements or a different geometry space are. An
    approved approval without a snapshot fails closed (`CUSTOMER_APPROVAL_OUTDATED`, `details.snapshot_stored=false`):
    imported legacy approvals must be renewed, which D2-13's re-draw requires anyway.
15. **Neutral link / termination point**: each field in NOT_AVAILABLE/NEEDS_MODIFICATION needs its own *required*
    additional-work item (`NEW_NEUTRAL_LINK`, `NEW_TERMINATION`; work types are compared upper-cased). Unrecorded
    (`None`) needs nothing yet.
16. **Additional work**: any required item REJECTED → `ADDITIONAL_WORK_REJECTED`; a required customer-impacting item not
    in NONE/APPROVED (and not rejected) → `ADDITIONAL_WORK_UNAPPROVED` (PLAN D-12, Plan 2 D2-3). Internal work never
    waits for the customer.
17. **State validation**: every enum of `InspectionState` and its parts must be the stored (upper-case) value; one
    assessment per type, one current annotation per type, unique approval numbers. `normalise_suitability` /
    `normalise_availability` translate legacy spellings for the importer; an unknown suitability becomes `None`
    (unconfirmed), never a pass.

## Readiness: the 19 steps (PLAN §2.9 order)

| Step | Code(s) | Text (legacy wording kept where one existed) |
|---|---|---|
| 1–3 | `CUSTOMER_REQUIRED`, `ENGINEER_REQUIRED`, `VISIT_DATE_REQUIRED` | Customer is required · Assigned engineer is required · Site visit date is required |
| 4–5 | `PANEL_PHOTO_REQUIRED`, `EQUIPMENT_PHOTO_REQUIRED` | Proposed panel-area reference photo is required · Proposed inverter + ACDB + DCDB reference photo is required |
| 6–9 | `PANEL_WIDTH_REQUIRED`, `PANEL_HEIGHT_REQUIRED`, `EQUIPMENT_WIDTH_REQUIRED`, `EQUIPMENT_HEIGHT_REQUIRED` | … measured width/height is required (positive number) |
| 10 | `LOCATIONS_NOT_DOCUMENTED` (`details.missing`, `details.legacy_geometry`) | Both proposed installation locations must be documented |
| 11 | `RESTRICTIONS_NEED_REMARKS` | Location restrictions require customer remarks |
| 12 | `SUITABILITY_UNCONFIRMED` / `SITE_NOT_SUITABLE` / `SUITABILITY_CONDITIONAL` | Site suitability has not been confirmed · Site is not suitable for solar installation · Conditional site suitability requires resolution before installation |
| 13 | `CUSTOMER_APPROVAL_REQUIRED` / `CUSTOMER_APPROVAL_OUTDATED` | Customer-approved installation location is required · Customer approval is outdated because the proposed installation location changed |
| 14 | `ENGINEERING_REVIEW_PENDING` | Engineering review is required for this site |
| 15 | `ADDITIONAL_WORK_REJECTED`, `ADDITIONAL_WORK_UNAPPROVED` | Additional work/cost has been rejected · Customer-impacting additional work is awaiting the customer's approval |
| 16 | `EQUIPMENT_SYSTEM_TYPE_UNDECIDED` (DV-17), `EQUIPMENT_<TYPE>_INCOMPLETE`, `EQUIPMENT_<TYPE>_NEEDS_RESOLUTION` | System type must be decided before the equipment can be assessed · `<TYPE WITH SPACES>` assessment is incomplete · … requires engineering resolution |
| 17 | `WHEELING_CONSUMER_NUMBER_REQUIRED`, `WHEELING_PHONE_REQUIRED` | Consumer Number is required for wheeling · Registered Phone is required for wheeling |
| 18 | `NEUTRAL_OR_TERMINATION_DECISION_REQUIRED` (`details.fields`) | Neutral Link / Termination Point requires an additional-work decision |
| 19 | `LATEST_APPROVAL_NOT_APPROVED` | Latest installation location approval is not approved |

`field_completion` (enforced at submit, DV-18): `CUSTOMER_REQUIRED`, `ENGINEER_REQUIRED`, `VISIT_DATE_REQUIRED`,
`SITE_ADDRESS_REQUIRED`, photos, the four measurements, `LOCATIONS_NOT_DOCUMENTED` (missing only), `SUITABILITY_UNCONFIRMED`
(any recorded verdict passes), `COMPLEXITY_NOT_ASSESSED`, `ENGINEERING_REVIEW_PENDING`, `EQUIPMENT_<TYPE>_INCOMPLETE`;
warning `LEGACY_ANNOTATION_GEOMETRY` (Plan 2 D2-13: re-draw before release).

## Site Inspection V2 spec §J — disposition

| §J | Defect | Where it is fixed | Evidence |
|---|---|---|---|
| 3 | `installation_readiness` forgeable | derived by `evaluate`; `InspectionState` has no such field | `test_readiness_is_judged_on_the_row_after_the_change` |
| 7 | approval snapshot never stored / invalidation dead | `build_location_snapshot` + `location_snapshot_matches` | scenarios `location_moved_*`, `approval_imported_without_snapshot`, `rectangle_resaved_unchanged`; snapshot tests |
| 10 | no enum/value validation | enum-validated state, `validate_results` | `test_state_is_validated`, `test_parts_are_validated`, `test_validate_results` |
| 13 | fresh PA inspection UNDECIDED hides the checklist | UNDECIDED blocks release (DV-17); the PA linking itself is the site_inspections package's | scenario `undecided_system_type` |
| 16 | status over submitted keys; no waiver | `compute_status` over the full definition; RESOLVED/WAIVED resolve | scenarios `one_pass_tap`, `failed_check*`, `review_resolved`; `test_full_definition_status_differs_only_on_partial_answers` |
| 17 | equipment route read the snapshot, readiness the column | `required_equipment_types(system_type column)` only; `normalise_results` gives the full-replace PUT shape | scenario `system_type_only_in_snapshot` |
| 18 | neutral link/termination collapsed, spelling never matched | two enum fields, one decision each | scenarios `neutral_*`, `termination_needs_modification_only`; `test_neutral_link_and_termination_each_need_their_own_decision` |
| 19 | CONDITIONAL stored as 0; unknown strings pass | `Suitability` enum; `normalise_suitability` (recommendation first; unknown → unconfirmed) | scenarios `conditional_*`, `unknown_suitability` |
| 20 | completion not enforced, demanded SUITABLE, ignored equipment | `field_completion` (enforcement is the submit transition's) | scenarios `not_suitable`, `conditional_flag`, `equipment_missing`, `one_pass_tap`, `many_blockers_in_order` |
| 21 | engineering review had no resolution | review decisions read by `engineering_review_pending` | `test_engineering_review_pending` |
| 22 | additional work inferred for almost every inspection; approval never gated | explicit items with `required`; `ADDITIONAL_WORK_UNAPPROVED` | scenarios `structure_type_differs_rejected`, `customer_impacting_work_*`; `test_additional_work_rules` |
| 23 | readiness on the pre-update row | pure evaluation of the state given; `InspectionState.with_changes` | `test_readiness_is_judged_on_the_row_after_the_change` |
| 30 | `hasValue(0)` false; container-relative rectangles | `has_value`, `is_positive`; LEGACY_CONTAINER blocks release | scenario `wheeling_consumer_number_zero`, `legacy_container_rectangles`; `test_has_value_zero_is_a_value` |
| 35 | dead helpers | their intent is implemented: `validate_results`/`evidence_required` (validateEquipmentAssessment), `is_resolved` (hasUnresolvedCriticalIssue), `location_snapshot_matches` (isApprovedLocationCurrent) | unit tests |
| 1, 2, 4–6, 8, 9, 11, 12, 14, 15, 24–29, 31–34, 36–39 | auth, serializers, persistence, linking, media, report, UI | not engine-level: site_inspections / media / documents packages | — |

## Legacy mappings

eSSL `attendance` (v3) → `attendance_day` / `DayResult` (v4):

| v3 | v4 | Note |
|---|---|---|
| `first_in`, `last_out`, `punch_count`, `working_minutes`, `break_minutes`, `late_minutes`, `early_exit_minutes`, `overtime_minutes`, `is_late`, `is_early_exit`, `status`, `source_raw_ids` | same names | `punch_count`/`source_raw_ids` exclude debounced punches (A3) |
| `device_id` (first punch) | `first_device` | first *accepted* punch |
| — | `worked_on_off_day`, `leave_conflict`, `ignored_raw_ids`, `missing_out` (derived) | A4, A3 |
| `is_manual_override` | `is_corrected` + `attendance_correction` | A12 |
| `processing_version` `v3-half-day-arrival` | `v4` | history is recomputed (PLAN §7.5) |
| status codes `P L A HD WO H Leave` | `P LT A HD WO H L` | A9 |
| `attendance_rules.rules` keys | `hr_attendance_rule.rules`, same three keys | `validate_rules_payload` |
| `attendance_raw.dedup_key` `dev{id}:uid{uid}` | `punch_dedup_key(...)` | A11 |

Site Inspection V2 (SQLite) → `InspectionState` — the mapping `engines/tests/inspection_cases.to_state` applies,
offered to the site_inspections importer: `customer_id`/`engineer_id` → uids; `site_visit_date` → `visit_date`;
`panel_width`… → `…_m`; `has_location_restrictions` via legacy `asBoolean`; `final_recommendation` + `site_suitable_for_solar`
→ `normalise_suitability`; `neutral_link`, `termination_point` → `normalise_availability` each; `wheeling_required`
`Yes/No` → bool; `registered_phone` → `registered_phone_e164` (E.164 normalisation is the importer's);
`system_type` column only (UNDECIDED when empty); annotation `geometry_json {x,y,width,height}` → `{x,y,w,h}` with
`geometry_space=LEGACY_CONTAINER`, `metadata_json {widthM,heightM,areaM2}` → `width_m/height_m/area_m2`; approval
`version` → `number`; equipment `results_json` → `results` (status recomputed; `legacy_status` reports the old one),
`resolution_status` RESOLVED → RESOLVED, WAIVED_APPROVED → WAIVED; flag columns (`walkway_required` → WALKWAY,
`ladder_required` → LADDER, `sliding_door_required` → SLIDING_DOOR, `underground_cabling` → UNDERGROUND_CABLING,
`extra_ac_cable`/`extra_dc_cable` → EXTRA_AC_CABLE/EXTRA_DC_CABLE, `new_neutral_link` → NEW_NEUTRAL_LINK,
`new_termination` → NEW_TERMINATION, `additional_earthing_required` → ADDITIONAL_EARTHING, `civil_work` → CIVIL_WORK,
`other_additional_work` → OTHER) → required additional-work items with the legacy `additional_work_status`.

## Parity evidence

Both legacy engines were run unmodified (read-only sources; only their persistence imports replaced) over the same
case tables the v4 tests use; the outputs are committed and every test run compares against them.

* **eSSL v3** — `scripts/parity/capture_essl_v3.py` runs `backend/app/services/processing.py`
  (sha256 `ec0c3d51…afb289d`, `v3-half-day-arrival`) with stubbed SQLAlchemy/model imports.
  102 day cases (ported from `test_attendance_processing.py` 18, `test_half_day_and_in_out.py` 18,
  `test_employee_list_active_default.py` 11, `test_multi_device_attendance.py` 8, `attendance_logic_test.py` 23,
  spec §C5 worked examples 7, explicit v4 cases 17): **72 identical to v3 in all 12 compared fields; 30 differ, each
  only in the fields its case documents, with the v3 value quoted** (A2 16, A5 7, A3 5, A4 5; a case may cite two).
  15 deadline cases (5 differ, all A5), 10 attribution cases (3 differ, all A6), break deduction for gross 0–600
  (differs exactly on 61–299, A2). The seven §C5 worked examples are identical in v3 and v4.
* **Site Inspection V2** — `scripts/parity/capture_si_legacy.py` + `si_legacy_runner.mjs` run `lib/site-inspection.ts`
  (sha256 `1943dfe6…fc33939`) and `lib/equipment-assessment.ts` (sha256 `f638ff39…e09d`) under Node 22 type stripping
  with an in-memory read-only fake of `./db`. 54 scenarios: readiness identical in 36, different in 18 — each a
  documented §J/§C fix or decision; field completion identical in 43, different in 11 (documented). The legacy
  checklist definitions equal `DEFINITIONS["v1"]` word for word (14/16/19); `legacy_status` reproduces the legacy scorer on
  all 15 answer maps; the full-definition status differs only on partial answers.

## Legacy import contract

Not applicable: engines-ops owns no tables. The mapping above (and `normalise_suitability`, `normalise_availability`,
`legacy_status`, `punch_dedup_key`, `recompute`) is what the hr/attendance/devices and site_inspections importers use.

## Hand-over notes

* **attendance/hr/devices** — build `Shift` from `hr_shift` (`key` = shift uid for rule matching; JSON day lists are
  accepted); `Employee(key, office, timezone=office.timezone, shift=effective_shift(own, office default), joined_on,
  left_on)`; `RawPunch(raw_id, device uid, pin, punch_at)`; `DeviceLink` from `devices_device_user`; pass
  `corrected` = days with an unrevoked correction. Write `result.as_dict()` fields; `skipped_future` is expected
  (A7); `unmapped` feeds the per-device unmapped list. `finalise_day` at 00:30 office time recomputes
  `finalise_target(now, tz)`. Use `affected_work_dates` + the previous/next day for event-driven recomputes. Reports:
  `calendar_fill` → `summarise` per employee → `combine_summaries` per office; `STATUS_CODE`/`STATUS_LABEL` everywhere.
  Office timezone validation: `is_valid_timezone`. Rule API validation: `validate_rules_payload`.
* **site_inspections** — build `InspectionState` from the *post-update* row (`state.with_changes(**validated)`),
  current annotations, approvals, assessments, additional-work items and reviews; gate `release` on `evaluate(...).ready`
  and `submit` on `field_completion(...).ready`; store `build_location_snapshot(annotations)` on every approval request;
  `PUT …/equipment/{type}/` validates with `validate_results` and stores `normalise_results` + `checks_version`; require an
  evidence photo when `evidence_required(status)` (D2-4); use `NEW_NEUTRAL_LINK` / `NEW_TERMINATION` as work types.
