# attendance — raw punches, processed days (engine v4), corrections, calendars, reports, dashboards

Work package `attendance` builds the `attendance` app of PLAN §2.9 (`attendance_*`, A1–A12) on top of `hr`, `devices`
and `engines.attendance`: the punch store behind devices' ingestion, the recompute pipeline (outbox → debounced Celery,
`finalise_day`), corrections, the staff endpoints of §3.4 "Attendance", the `attendance` record scopes of §3.2, the
consumers of §3.5 (`attendance.punches_ingested`, `hr.attendance_inputs_changed`) and the eSSL import of §7.5 (raw
punches + the v3/v4 status diff for HR sign-off). Legacy: eSSL `services/processing.py`, `calendar_service.py`,
`reports.py`, `exporters.py`, `attendance_source.py`, `attendance_sync.py`, routers `attendance`, `reports`,
`dashboard` (spec `essl-attendance-spec.md`; none of its §I defects are carried over). Deviations: DV-87 … DV-91.

## What exists

| Area | Where | Notes |
|---|---|---|
| Models | `attendance/models/{punch,day,recompute,fields}.py` | `attendance_raw_punch` *(no base, append-only, partitioned — DV-87)*, `attendance_day` (every §2.9 column; PU `(employee, work_date)` among live rows; status CHECK; minutes ≥ 0; OUT needs IN and is not before it; `missing_out` derived and checked), `attendance_correction` (+ `revoke_reason`, one active correction per field — DV-90), `attendance_recompute_request` *(no base — DV-88)*. `WallClockDateTimeField` stores `timestamp` (wall clock) columns and refuses an aware value. |
| Raw SQL | `attendance/migrations/0001_initial.py` | the partitioned table, its DEFAULT partition, keys, indexes, the first three monthly partitions and the append-only REVOKE for `DB_APP_ROLE` (like `audit_log`). |
| Punch store | `attendance/services/sink.py` | `RawPunchSink` registered in `AttendanceConfig.ready` as `devices.services.punch_sink` (DV-78): `INSERT … ON CONFLICT (dedup_key, device_time) DO NOTHING RETURNING` — the agent upload and the ADMS push of one punch are one row; `status`, `pin_activity`, `observed_codes` for the devices screens. |
| Partitions / privileges | `attendance/services/partitions.py`, `manage.py maintain_attendance_punches` | monthly partitions on `device_time` (stranded DEFAULT rows moved in), append-only privileges (app role: SELECT/INSERT only, partitions closed); run by `deploy/release.sh` as the owner and monthly by Beat when the worker may. |
| Engine inputs | `attendance/services/inputs.py` | one place builds `Shift`/`Employee`/`Holiday`/`Leave`/`AttendanceRule`/`DeviceLink`/`RawPunch` from `hr_*`, `devices_device_user` and the punch store. |
| Recompute | `attendance/services/recompute.py` | the single writer of `attendance_day` (engine v4); debounce queue (`request_recompute`, `run_due`); `finalise_due` (per office clock). |
| Corrections | `attendance/services/corrections.py` | create / revoke (A12), `deny_self_action`, engine validation, employee lock shared with the recompute. |
| Reads | `attendance/services/{days,calendar,reports,exporters,dashboard,processing,scopes}.py` | lists and origins, the one calendar fill (A9), provisional today, reports + CSV/XLSX/PDF, scoped dashboards, staff-triggered recomputes, scope filters. |
| Tasks | `attendance/tasks.py` | `run_due_recomputes` (on commit + Beat every minute), `finalise_days` (Beat every 15 min), `maintain_punch_partitions` (Beat monthly). |
| Events | `attendance/events.py` | `attendance.punches_ingested` (devices), `hr.attendance_inputs_changed` (hr, devices) → debounced recompute requests. |
| Providers | `attendance/services/dashboard.py` (installed by `ready`) | hr `office_summary["attendance"]`, `employee_dependencies["attendance"]` (`attendance_days`, `raw_punches`), `GET dashboard/` counters of the `attendance` module. |
| Documents | `attendance/templates/documents/attendance_report/` | the `ATTENDANCE_REPORT` template (A4, landscape above 8 columns). |
| Import | `attendance/services/legacy_import.py` | see "eSSL import". |
| Fixtures | `attendance/tests/legacy/` | `capture_essl.py` (how they were made), `essl_attendance_tables.json` (masked `SELECT *` of 13 eSSL tables, incl. v3's own stored days). |

## Endpoints (`/api/v1/`, module `attendance`, record scope all / office / self)

| Path | Permission |
|---|---|
| `attendance/days/` list (`date_from`, `date_to`, `employee`, `office`, `status`, `is_corrected`, `missing_out`, `include_inactive`, `search`, `ordering`) / `…/<uid>/` | view |
| `attendance/raw/` (cursor, newest first; `device`, `pin`, `employee`, `date_from`, `date_to` — terminal-local dates) | view |
| `GET attendance/employees/<uid>/timeline/?work_date=` | view |
| `GET attendance/calendar/?employee=&year=&month=` · `…/calendar/all/?year=&month=&office=&employee=&search=&include_inactive=` (≤ 300 employees, else 400 `too_many_employees`) | view |
| `GET attendance/day/?day=&office=&employee=&search=&include_inactive=` · `GET attendance/date-ranges/?office=` | view |
| `POST attendance/process/` (≤ 93 days), `POST attendance/process-all/` (202, queued), `POST attendance/recalculate/` | manage |
| `attendance/corrections/` list / `…/<uid>/` | view |
| `POST attendance/corrections/` (`day_uid`, `field`, `new`, `reason`, `expected_version` = the day's) · `POST …/<uid>/revoke/` (`reason`, `expected_version`) | edit (never on your own day: 403 `self_action_denied`) |
| `GET attendance/reports/{daily,weekly,monthly,monthly-detail,monthly-individual,individual,office}/?format=json\|csv\|xlsx\|pdf` | export |
| `GET attendance/dashboard/summary/?day=&office=`, `…/recent-punches/?limit=&office=`, `…/trend/?days=&office=` | view |

Every list is paginated (days/corrections: page numbers; raw: cursor) and filtered; every endpoint is in the OpenAPI
schema with its error envelopes; error codes: `validation_error`, `range_too_long`, `too_many_employees`,
`correction_exists`, `correction_revoked`, `correction_revoke_conflict`, `stale_version`, `self_action_denied`, `not_found`,
`permission_denied`.

## Decisions not spelled out in the PLAN

1. **Partitioned on the terminal clock** (DV-87). `device_time` is part of the hashed content, so the unique key the
   partitioning forces, `(dedup_key, device_time)`, is exactly "one row per content hash" (A11). Readers bound
   `device_time` (a punch belongs at most a day either side of its work date in any zone, `inputs.device_time_window`)
   so every query prunes partitions. The model is managed (its DDL is raw SQL) so test flushes truncate it with
   `devices_device`.
2. **One writer, one lock.** `recompute()` locks the people it recomputes (`SELECT … FOR UPDATE` of `hr_employee`), and
   so does a correction before it locks the day: a recompute and a correction of the same person serialise, and two
   recomputes never collide on `(employee, work_date)`. Only changed days are written (their `version` moves, so a
   correction made against an older reading is `stale_version`); a stored day the engine no longer produces for the
   person (outside `joined_on`/`left_on`) is soft-deleted unless corrected. Who is recomputed: live employees who are
   active or left on/after the range start.
3. **Today is never stored** (A7). The recompute clamps the range to the latest possible "today" (UTC+14) and the
   engine skips each person's own today; the calendar shows today and later blank (`PENDING`); `finalise_due` runs every
   15 minutes and recomputes each office's newly final day once after 00:30 on that office's clock (plus the day
   before, for late uploads); a cache marker makes it once per office and date, and repeating it is harmless.
   `attendance/day/` and the dashboard show today **provisionally** from the punches received so far (`PROVISIONAL`,
   computed in memory by `compute_day`, punches later than now ignored); nobody-in-yet is blank, never ABSENT.
4. **Debounce** (A8, DV-88). Handlers only record requests; `run_due` merges the due ones (everyone / per office / per
   employee grouped by identical windows) and runs them in one transaction; a failure keeps them with `attempts` and
   `last_error` and retries after 5 minutes. `attendance.punches_ingested` recomputes the linked people from the day
   before the first punch date to the day after the last (overnight shifts, zones); an event with only unmapped PINs
   queues nothing (the later link change emits `hr.attendance_inputs_changed`). Ranges are capped at
   `ATTENDANCE_MAX_RECOMPUTE_DAYS` (400), the latest days kept and the cut reported (`not_recomputed_before`).
5. **Scopes**: the same definitions as hr (`self` = the employee linked to the caller, `office` = the office of the
   caller's own employee record; fail closed without one). One filter per scope serves employees, days, corrections
   and raw punches — a punch belongs to whoever its `(device, pin)` is linked to (A1). "Office" follows the employee's
   current office.
6. **One calendar fill for everything** (A9). Calendars, rosters, the day view, the trend and **every report** read
   `engines.attendance.calendar_fill` over the stored days; the daily/weekly/individual/office reports no longer read
   stored rows only. One formula: `attendance_rate` for a person, an office (pooled counts) and the overall line alike;
   leave, holidays and weekly offs are outside the denominator. Codes `P LT A HD WO H L` everywhere (the weekly grid
   prints H:MM when time was worked, else the code: `H` is only ever a holiday).
7. **Where a day came from** is read back from its `source_raw_ids` (every contributing terminal by its registered
   label, its office, the transports) in one query per page — never from the single `first_device` column (§I.17).
8. **Corrections** (A12, DV-90): fields `status`, `first_in`, `last_out` (office wall clock, no zone), the minute
   fields and the two flags, validated by `engines.attendance.apply_correction`; `missing_out` follows; the day is
   pinned (`is_corrected`). Revoke restores `old` (refused with `correction_revoke_conflict` when it would contradict
   another active correction, e.g. an OUT without an IN) and, when nothing is left on the day, un-pins it and queues
   its recompute immediately. `attendance.edit` is never granted on your own day (`deny_self_action`).
9. **Files.** CSV (UTF-8 BOM) and XLSX carry title, subtitle, header, rows and a Summary block like eSSL; every text
   cell that a spreadsheet would evaluate (`= + - @ \t \r`) is prefixed with `'` (§I.7). PDFs and reports over 5,000
   rows are `ATTENDANCE_REPORT` render jobs (DV-89) whose object is a fresh report uid (`attendance.report`); the
   documents default access rule applies (`attendance.export`; the requester or an `all`-scope reader). DRF's `?format=`
   renderer override is disabled on the report views (it would answer 404 for `csv`).
10. **Dashboard** is scoped (§I.1): counts cover the caller's people; the per-office terminal block
    (`devices.services.providers.office_summary`) appears only for callers with `devices.view`; recent punches are the
    caller's scope, newest by the instant (a Dubai punch at 08:55 is later than a Kolkata one at 09:31).
11. **Recalculate** (eSSL dialled each SERVER_PULL terminal first): the platform never dials a terminal, so it
    recomputes the window (default yesterday) and reports per terminal whether its punches arrive by agent, ADMS or not
    at all, with the last sync and punch times.

## Legacy mapping (eSSL → platform)

| eSSL | Platform | Rule |
|---|---|---|
| `attendance_raw.device_id` | `attendance_raw_punch.device` | via the devices import's map; unmapped device → skipped (violation) |
| `device_user_id` | `pin` | string, stripped, ≤ 80 |
| `punch_time` (naive) | `device_time` (naive) + `punch_at` | `punch_at` = the reading in the device office's zone (A10) |
| `status`, `punch` | `status_code`, `punch_code` | beyond smallint → kept in `raw_payload._essl` only (violation) |
| `dedup_key` (`dev{id}:uid{uid}`, `dev…:u…:t…` for ADMS) | `dedup_key` = sha256(serial\|pin\|device_time\|status\|punch) | the key the agent and the ADMS receiver compute (A11); eSSL's kept in `raw_payload._essl.dedup_key`; equal content collapses (`collapsed`) |
| `source` (ZK_PULL, AGENT_PUSH, ADMS_PUSH) | `source = IMPORT` | the original in `raw_payload._essl.source` |
| `agent_id` | `agent` | via the devices import's map |
| `device_serial`, `device_record_uid`, `raw_payload`, `received_at` | same | the platform device's serial wins (the key must match live deliveries); NULs scrubbed |
| `is_processed` | — | derived (a day's `source_raw_ids`) |
| `attendance` (v3 days) | — (recomputed) | not imported: v4 recomputes the covered range; `status_diff_report` lists per employee and month the v3 and v4 status counts and every day whose status differs |
| `attendance.is_manual_override` | — | no eSSL API could set it; reported if present, the day is recomputed |
| `processing_version` `v3-half-day-arrival` | `v4` | |
| `device_id` (first punch) | `first_device` | first *accepted* punch |

## Parity evidence

* **Fixtures from the real app.** A private copy of a seeded eSSL database (`essl_wp_attendance`, restored from the
  eSSL alembic chain + `scripts/seed.py`) and a private eSSL server on 127.0.0.1:18153 were driven only through eSSL's
  staff API, agent protocol and ADMS receiver (`capture_essl.py`), then eSSL's own `POST /api/attendance/process`
  stored its v3 days for August 2026; 13 tables were exported read-only and masked (names hashed, e-mails/phones and
  password/token hashes dropped). `legacy_goldenapp` / `legacy_blog_cms` were never touched.
* `attendance/tests/test_legacy_import.py` imports hr, devices and attendance from those tables: 35 eSSL raw rows → 34
  punches, the ADMS push and the agent upload of the 18 August 09:30 punch **collapsed** and reported; an agent
  re-delivering an imported punch after the cutover is a duplicate; a re-run creates nothing. The recompute compares
  **155 employee-days (5 people × 31)**; exactly **5 differ**, each a documented v4 change:

  | Employee | Date | v3 | v4 | Why |
  |---|---|---|---|---|
  | E001 | 2026-08-15 | LATE | HOLIDAY (worked) | A4 |
  | E001 | 2026-08-16 | PRESENT | WEEKLY_OFF (worked) | A4 |
  | E002 | 2026-08-03 | LATE | HALF_DAY | A5: 11:40 on an 11:00 shift is past start + 30; v3's 10:00 wall-clock deadline rolled to the next day |
  | E003 | 2026-08-05 | PRESENT | ABSENT | A6: the 06:45 OUT of the night shift belongs to the 4th |
  | 5 | 2026-08-03 | PRESENT | ABSENT | A1: no `employee_code` fallback for the unlinked PIN 5 |

  Field-level differences on unchanged statuses are pinned too: the double scan is one accepted punch + one ignored
  (A3), the 61-minute day is 61 working minutes (v3: 1, A2), the full-day leave with punches is flagged
  `leave_conflict` (A4), the day with eSSL's stored duplicate counts 2 punches (v3: 3, A11). E004's PIN 4 on the second
  terminal (linked there by eSSL's device-less `map-pin`) keeps its link — the devices import reports it for HR to
  confirm — so no status changes.
* The engine's own golden parity (102 v3 day cases) is engines-ops' (`engines/tests/test_attendance_parity.py`).

## Shared changes

* `flarize/settings/base.py`: `SPECTACULAR_SETTINGS["ENUM_NAME_OVERRIDES"]` += `AttendanceStatusEnum`,
  `AttendanceStatusCodeEnum`, `AttendanceCorrectionFieldEnum`, `AttendanceTransportEnum`, and names for the three
  existing sets whose field names now collide (`DeviceProtocolFieldEnum`, `SeoRedirectStatusCodeEnum`,
  `DeviceTransportEnum`; wire formats unchanged); `CELERY_BEAT_SCHEDULE` += `attendance.run_due_recomputes` (60 s),
  `attendance.finalise_days` (every 15 min), `attendance.maintain_punch_partitions` (monthly).
* `deploy/release.sh`: `manage.py maintain_attendance_punches --months 3` after `ensure_inventory_append_only` (owner).
* `hr/tests/test_offices_api.py`, `hr/tests/test_employees_api.py`: the tests that register stand-in providers now
  swap the registry's provider dict with `monkeypatch` instead of `unregister(...)`, which removed the providers the
  attendance and devices packages install (an order-dependent failure for any test running after them); the summary
  test states hr's own part with no package sections.

## Open issues

* ADMS stays behind `ADMS_RECEIVER` (devices D-10); the ATTLOG column mapping is still UNPROVEN.
* Large exports are PDF only (DV-89); CSV/XLSX beyond 5,000 rows need a narrower selection until the documents package
  can store other file types.
* The weekly ops report has no attendance section (pending recompute requests are visible in the recompute queue
  table; a `/healthz` check was not added — core pins the set of checks).
