# Wave-3b integration (attendance)

Merged into `claude/bold-goodall-lxgwep` with `git merge --no-ff`: `wp/attendance` (tip `1470d2c`, which had already
merged the integration branch at `a24b532`). Fast checks after the merge (`check --fail-level WARNING`,
`makemigrations --check`, `lint-imports`, the attendance/hr/devices/procurement tests), then every gate once on the
result (black, isort, flake8, `check --fail-level WARNING`, **`check --database default --fail-level WARNING`**,
`makemigrations --check`, `lint-imports`, `spectacular --validate --fail-on-warn`, view budget, on-commit enqueue
check, `bash -n` of the deploy scripts, the full pytest suite).

## Deviation numbers

attendance already renumbered its rows onto the integration branch before the merge: bom keeps DV-87 … DV-90 and
attendance uses DV-91 … DV-95 (`docs/DEVIATIONS.md`, `docs/decisions/attendance.md`, `attendance/*.py`). No duplicate
number remains. attendance's DV-78 references are to the devices punch-sink row, which is correct.

## Wiring

* The punch store is registered by attendance itself (`AttendanceConfig.ready` → `devices.services.punch_sink`,
  DV-78); nothing to wire at integration.
* No requirements changed; attendance is the only package adding migrations to its app, so no merge migration; no
  engines module was duplicated. `flarize/settings/base.py` merged cleanly.

## Release blocker fixed: identifier lengths (models.E034)

`procurement.Batch` declared the index `procurement_batch_status_created` (32 characters). Django's database checks
reject index names over 30 characters (models.E034), and `manage.py migrate` runs those checks, so a fresh database
could not be migrated (release step 2). Plain `manage.py check` does **not** run database checks, which is why every
earlier gate was green.

* Renamed to `procurement_batch_status_idx` in `procurement/models/batch.py` and in the unreleased
  `procurement/migrations/0001_initial.py` (edited in place: nothing is deployed; procurement has no later migration
  and nothing else referenced the old name).
* Sweep of every installed model: no other index name exceeds 30 characters; no index, constraint, table or column name
  exceeds PostgreSQL's 63-character limit (the longest constraint name is 57). Constraint names over 30 characters are
  not a Django check failure on PostgreSQL and are left alone.
* Proof: on a brand-new empty database, `check --database default --fail-level WARNING` is clean, a full `migrate`
  applies, `migrate procurement zero` / `migrate procurement` and `migrate attendance zero` / `migrate attendance`
  round-trip, and `migrate --check` reports nothing unapplied.
* Regression test: `core/tests/test_database_checks.py` runs the database system checks and bounds index (30) and
  constraint (63) name lengths.

**Future gates must include `python manage.py check --database default --fail-level WARNING`** in addition to the plain
check. It is now a CI step in `.github/workflows/ci.yml` (run against the service's `postgres` database, because the
database checks connect and `flarize_ci` itself is never created).

## Left open (business/ops questions, not integration wiring)

See the attendance package notes in `docs/decisions/attendance.md`: ADMS stays behind `ADMS_RECEIVER` with the ATTLOG
column mapping unproven; exports over 5,000 rows are PDF only (DV-93); pending recompute requests are not surfaced in
`/healthz` or the weekly ops report; `process/` and `recalculate/` do not apply record scope to `employee_uids`;
recomputed history follows the employee's current office; unbounded `device_time` on the origins lookup; recompute
dedup relies on Redis (`cache.add`).
