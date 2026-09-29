# hr — offices, shifts, employees, holidays, leave, attendance rules

Work package hr builds the `hr` app of PLAN §2.9 (`hr_*`), its staff endpoints (§3.4 "HR"), the `office`/`self`
record scopes of `employees` and `leave` (§3.2), the Employee ↔ User link with Staff role automation (DV-1, §3.2
Staff), the events of §3.5 (`hr.employee_deactivated`) and A8 (`hr.attendance_inputs_changed`), and the eSSL
importer (§7.5). Deviations: DV-49 … DV-53 in `docs/DEVIATIONS.md`.

## What exists

| Area | Where | Notes |
|---|---|---|
| Models | `hr/models/{office,shift,employee,calendar}.py` | `hr_office`, `hr_shift` (every §2.9 column incl. the v4 `overnight_buffer_minutes`, `half_day_after_minutes`, `debounce_minutes`), `hr_employee` (`user` OneToOne, DV-1; `photo` → private media), `hr_holiday` (+`notes`, DV-49), `hr_leave_type`, `hr_leave_record` (`type_id` column), `hr_attendance_rule`. Codes unique among live rows, case-insensitive (DV-50); enum checks; DB checks for the shift clock/overnight consistency, minute bounds, half day ≤ full day, `left_on ≥ joined_on`, leave date order, half day = one date, a rule scoped to at most one of office/shift; both holiday partial uniques. |
| Services | `hr/services/{offices,shifts,employees,employee_links,holidays,leave,rules,recompute,scopes,dashboard,validation,common,legacy_import}.py` | every write `@transaction.atomic`, versioned (compare-and-swap), audited (`hr.*`), cache-bumped (`hr:setup`, `hr:employees`, `hr:leave`). |
| Registries | `hr/registries.py` | `office_summary` sections, `employee_dependencies` / `office_dependencies` counters, `device_mappings` and `device_reconciler` providers — filled by the attendance and devices packages (hr imports neither). |
| Scopes | `hr/services/scopes.py` | `employees.self/office`, `leave.self/office` (registered in `HrConfig.ready`). |
| Dashboard | `hr/services/dashboard.py` | `employees` (active/inactive), `leave` (pending, on leave today), `hr_setup` (active offices/shifts) — scoped. |
| Media | `hr/apps.py` | folder `hr/employees` reserved (photos never appear in the media library); `Employee.photo` registered with `media.usage`. |
| Hasher | `accounts/hashers.py` (shared, DV-53) | `LegacyBCryptPasswordHasher`: Django `bcrypt$` format with eSSL's 72-byte truncation. |
| Legacy fixtures | `hr/tests/legacy/` | `capture_essl.py` (how they were made), `essl_tables.json` (masked `SELECT *` of 8 eSSL tables), `essl_api.json` (masked eSSL API responses). |

## Endpoints (`/api/v1/`)

| Path | Permission |
|---|---|
| `hr/offices/` list (`is_active`, `search`, `ordering`) / detail / create / `PATCH` / `DELETE` (409 `office_in_use`) | `hr_setup` view · edit |
| `GET hr/offices/<uid>/summary/?day=` (default: today in the office's zone) | `hr_setup.view` |
| `hr/shifts/` list (`is_active`, `is_overnight`) / detail / create / `PATCH` / `DELETE` (409 `shift_in_use`) | `hr_setup` view · edit |
| `hr/employees/` list (`office`, `shift`, `is_active`, `include_inactive`, `has_login`, `identity_method`, `department`, `search`) / detail | `employees.view` (scope all/office/self) |
| `POST hr/employees/` | `employees.create` |
| `PATCH hr/employees/<uid>/`, `…/link-user/`, `…/unlink-user/`, `POST`/`DELETE …/photo/`, `POST hr/employees/reconcile-devices/` | `employees.edit` |
| `…/deactivate/`, `…/activate/`, `DELETE` (only without history) | `employees.archive` |
| `GET …/device-mappings/`, `GET …/dependencies/` | `employees.view` |
| `hr/holidays/` list (`office` = its own + global, `is_global`, `is_active`, `year`, `date_from/to`) / detail / create / `PATCH` / `DELETE` (soft) | `hr_setup` view · edit |
| `hr/leave-types/` list / detail · create / `PATCH` / `DELETE` (409 `leave_type_in_use`) | `leave.view` (whoever files or decides leave) · `hr_setup.edit` |
| `hr/leave/` list (`employee`, `office`, `leave_type`, `status`, `date_from/to` overlap, `search`) / detail | `leave.view` (scope all/office/self) |
| `POST hr/leave/` | `leave.create` |
| `…/cancel/` | gate `leave.view`; own leave needs `leave.create`, anyone else's `leave.approve` |
| `…/approve/`, `…/reject/` | `leave.approve` (never on your own leave) |
| `hr/attendance-rules/` list (`scope`, `office`, `shift`, `is_active`) / CRUD | `hr_setup` view · edit |

Every PATCH and action takes `expected_version` (409 `stale_version`); every list is paginated and filtered;
every endpoint is in the OpenAPI schema with its error envelopes.

## Decisions not spelled out in the PLAN

1. **Record scopes.** `self` = the employee record linked to the caller's login (`hr_employee.user`) and its leave;
   `office` = the office of the caller's own employee record (the Office Manager must be an employee of the office
   they manage). A caller without a linked employee (or whose employee has no office) sees nothing under a narrow
   scope — fail closed like `core.scopes`. Writes act on the scoped queryset (another office's leave is 404).
2. **Staff role automation** (DV-51). `link-user/` takes `user_uid` (existing account) or `email` (creates a Staff
   account with an invitation, exactly like `users/`). The Staff role replaces the account's role only when that role
   grants nothing beyond Staff (a Sales Executive or an HR officer who is also an employee keeps the wider role —
   `User.role` is single); nobody changes their own role. The caller must be able to manage the account in `users/`
   (`ensure_can_manage_user`: holds every grant of its role; Super Admins only by Super Admins) — required by the
   accounts `hr.employee_deactivated` handler, which acts as SYSTEM. An inactive account is re-linked only when its
   role is Staff (it is then reactivated). `unlink-user/` deactivates a Staff-role account (it exists only for the
   link — eSSL `revoke-login`), keeps any other account as it is, and is refused on your own login (it would let
   you approve your own leave).
3. **Deactivation.** `deactivate/` (optionally with `left_on`) emits `hr.employee_deactivated`
   `{"employee_uid", "user_uid" | null}` — the accounts handler deactivates the login and ends its sessions. Refused
   on your own record and on a record whose login you could not manage. `activate/` clears `left_on` and does **not**
   restore the login (eSSL behaviour): re-link it or reactivate it in `users/`. Deactivating an already inactive
   employee whose login is still active (eSSL's deactivation kept logins; the import reports them) emits the event
   again so the login is retired; otherwise it is a no-op. `DELETE` soft-deletes only an
   employee without history — leave records here plus the counters other packages register (attendance days, raw
   punches, device mappings) — and retires a linked login like a deactivation.
4. **Leave.** The new record is APPROVED when the caller may approve it (`leave.approve` and not their own record)
   or the type needs no approval (`requires_approval=false`), otherwise PENDING — PLAN's "Staff → PENDING, HR →
   APPROVED" generalised; `decided_by/at` are stamped. A request may not overlap another PENDING/APPROVED request of
   the same employee (409 `leave_overlap`, serialised by a row lock on the employee); approving re-checks overlap with
   APPROVED leave. `cancel/` (gate `leave.view`, decided in the service): the employee withdraws their PENDING leave,
   or APPROVED leave that has not started (`leave.create`; office-local today; else 409 `leave_already_started`); an
   approver (`leave.approve`: HR, or the Office Manager within their office) cancels anyone's PENDING/APPROVED leave.
   Leave types are read with `leave.view` (Staff and the Office Manager hold no `hr_setup`, yet need the types to file
   and decide leave; a lookup without record scope) and written with `hr_setup.edit`. There is no PATCH/DELETE (cancel and file again); `leave.archive` stays unused.
5. **`hr.attendance_inputs_changed`** (DV-52; A8). Payload `{"employee_uids": [...]}` or `{"office_uid": uuid | null}`
   (null = every office) plus `date_from`, `date_to` (inclusive, ISO) and `reason`. Leave and holidays carry their own
   dates; shift edits, office default-shift/time-zone changes, employee office/shift/date/activation changes and
   attendance-rule writes cover the last `HR_RECOMPUTE_LOOKBACK_DAYS` (default 31) days — a rule from its
   `effective_from` but never further back; a rule starting in the future emits nothing. Older history is
   re-computed explicitly (`attendance/process/`), so a rule edit never silently rewrites closed months. Renames emit
   nothing. Rule moves emit for the old and the new scope. The "link edits" of A8 are the per-device PIN links
   (A1), owned by the devices package: it emits the same event through `hr.services.recompute.for_employees(...,
   reason="device_link_changed")` (same HR context). The employee ↔ login link changes nothing the engine computes
   and emits nothing.
6. **Attendance rules JSON** accepts only `half_day_after` (`HH:MM[:SS]`, normalised), `half_day_after_minutes`
   (0–720) and `half_day_under_minutes` (0–1440); unknown keys, booleans and out-of-range values are 400 per key; at
   least one key. Both clock and minutes may be set (the clock wins, eSSL semantics). The shift response shows the
   deadline in force today from global and shift rules (`half_day_after`, `half_day_after_source` `rule`/`shift`;
   office rules depend on the employee and are applied by the engine).
7. **Office summary.** hr's own part: people (total/active), leave (on approved leave that day / pending covering
   it), the active holiday (an office holiday wins over a global one), weekly off and working day from the default
   shift by the engine's rule (`hr.services.common.is_working_day`, eSSL C3: no shift = Sunday off, weekly off wins,
   empty `working_days` = every other day works; a non-working day is a weekly off). `sections` holds whatever the registered providers return (attendance day counts, devices, agents, raw
   punches) — a failing provider is logged and left out. `device_count` of the eSSL office list moves there too.
8. **Photo.** `POST …/photo/` (multipart `file`) stores a PRIVATE `PHOTO` asset in `hr/employees` before the
   transaction (media rule), then attaches it under a row lock; a failed attach (stale version) deletes the new file;
   the replaced file is deleted. Responses carry 10-minute signed URLs (`photo.url`, `thumbnail_url`), never keys.
9. **Deletes.** Offices: refused while live employees or registered dependants (devices) remain; holidays and rules
   of the office are soft-deleted with it. Shifts: refused while employees or offices use them; their rules go
   with them. Leave types: refused while any record (even cancelled) uses them.
10. **Device endpoints** live under `hr/employees/` (the path hr owns) but are served by providers the devices
    package installs: `device-mappings/` answers `available: false` until then, `reconcile-devices/` 503
    `devices_unavailable` (and 400 `confirmation_required` for `apply` without `confirm`, as in eSSL).

## Legacy mapping (eSSL → platform)

`legacy_import.py` functions take `SELECT *` rows, track each source row in `core_legacy_map` (`ESSL`), re-run as
updates, report `{"created", "updated", "skipped", "violations"}` and write one `hr.legacy_imported` audit row per
call (counts + sha256 of the batch). Order: users → shifts → offices → employees → holidays → leave types → leave
records → attendance rules (`import_all`).

| eSSL | Platform | Rule |
|---|---|---|
| `roles.name` | `accounts_role` (seeded) | ADMIN → `admin`, HR → `hr`, USER/VIEWER → `staff`; no role / other → `staff` (reported) |
| `users.username`, `email` | `accounts_user.email` | e-mail is the login; missing/invalid → `<username>@migrated.invalid` (reported); an e-mail already used by a platform account links to it, nothing overwritten (reported) |
| `users.password_hash` (bcrypt) | `accounts_user.password` = `bcrypt$<hash>` | verified by `LegacyBCryptPasswordHasher` (72-byte truncation like eSSL), upgraded to Argon2 on the first login; a re-run never replaces an upgraded hash; not bcrypt, or a hash of eSSL's seeded default `admin123` (spec §B: "must be rotated") → unusable + `must_reset_password` (reported) |
| `users.full_name`, `is_active` | `first_name` / `last_name` (first word / rest), `is_active` | |
| `offices.*` | `hr_office` | `timezone` must be a zoneinfo name, else `Asia/Kolkata` (reported); `default_shift_id` via the shift map |
| `shifts.*` | `hr_shift` | columns 1:1; v4 columns take the PLAN defaults (180 / 30 / 2); `is_overnight` corrected when it contradicts the clock (reported); minute values outside the DB bounds → default (reported); `office_id` (informational in eSSL) not kept (reported); the A5 half-day deadline change (10:00 wall clock → start + `half_day_after_minutes`) reported per shift where it differs |
| `employees.employee_code` | `code` | case-insensitive unique |
| `employees.phone` | `phone_e164` | parsed with region IN; invalid → empty (reported) |
| `employees.email` | `email` | invalid → empty (reported) |
| `employees.attendance_identity_method` | `identity_method` | unknown → UNSPECIFIED (reported) |
| `employees.user_id` | `hr_employee.user` | via the users map; a login already linked to another employee is not linked twice (reported); an inactive employee with an active login (eSSL kept logins on deactivation) is reported |
| `employees.photo_url` | — | never applied in eSSL production; reported when present |
| `holidays.holiday_date`, `office_id`, `name`, `is_active`, `notes` | `hr_holiday.date`, `office`, `name`, `is_active`, `notes` | a second global holiday on a date (eSSL's PATCH allowed it) is refused (reported) |
| distinct `leave_records.leave_type` | `hr_leave_type` | code = upper-cased, non-alphanumerics → `_` (`Casual Leave` → `CASUAL_LEAVE`); strings differing only in case share a type (reported); `paid`/`requires_approval` true |
| `leave_records.*` | `hr_leave_record` | status 1:1; `decided_at` = source `updated_at` for decided rows; a multi-day half day → full days (reported); overlaps eSSL allowed are imported and reported |
| `attendance_rules.*` | `hr_attendance_rule` | unknown keys / invalid values dropped (reported); office **and** shift → shift rule (how eSSL applied it; reported) |

## Parity evidence

* **Fixtures from the real app.** A private database `essl_wp_hr` was migrated with the eSSL app's own alembic chain
  and seeded with its `scripts/seed.py` (venv `/home/user/.venvs/essl`, Python 3.12, the app's pinned requirements);
  a private eSSL server (port 18151) was then driven through its own API to create offices (incl. an invalid zone),
  shifts (incl. a mis-flagged overnight shift), employees with logins of every role, a revoked login, an inactive
  employee with an active login, holidays (incl. the duplicate global date PATCH lets through) and leave of every
  status (free-text types differing in case, a two-day "half day", an overlap). `attendance_rules` has no eSSL API
  and was inserted with SQL. `capture_essl.py` documents and reproduces every step and masks e-mails, phones, names
  and reasons deterministically; the passwords are synthetic test values.
* `hr/tests/test_legacy_import.py` (18 tests): per-table counts, every violation class, idempotent re-runs, updates
  of changed rows, deleted rows staying deleted, and **logins with the original eSSL passwords through
  `/api/v1/auth/login/`** — including an 80-byte password (eSSL truncation) — with the Argon2 upgrade.
* `hr/tests/test_legacy_parity.py` (5 tests): after the import, every field of the captured eSSL API responses
  (`/api/offices`, `/api/shifts`, `/api/employees`, `/api/holidays`, `/api/leave`) is served by the new endpoints,
  renamed per the field map in the test's docstring; the only differences are the repairs the import reports. The
  shift `half_day_after` / `half_day_after_source` values match eSSL exactly on these fixtures.

## Hand-over notes (attendance, devices)

* Register `office_summary` sections, `employee_dependencies` counters (`attendance_days`, `raw_punches`,
  `device_mappings`) and `office_dependencies` counters (`devices`, `agents`) in your `AppConfig.ready()`; install
  `device_mappings` / `device_reconciler` with `.set(fn)`.
* Consume `hr.attendance_inputs_changed` (payload above) and clamp to dates before today (A7). Devices: emit it
  for PIN link changes with `hr.services.recompute.for_employees(uids, date_from, date_to, reason="device_link_changed")`.
* Use `hr.services.rules.applicable_rules(office=, shift=, on=, candidates=)` + `merged()` for the engine's rule merge,
  `Employee.effective_shift`, `hr.services.common.today_in(office.timezone)`, and `hr.services.scopes.own_employee`
  for the `attendance` `self`/`office` scope filters (same definitions as here).
