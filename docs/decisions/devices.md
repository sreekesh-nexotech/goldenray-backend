# devices — office agents, terminals, device users, the agent protocol and the ADMS receiver

Work package `devices` builds the `devices` app of PLAN §2.9 (`devices_*`), its staff endpoints (§3.4 "Devices"), the
office-agent protocol (`/api/agent/v1/`, §3.4), the terminal receiver (`/iclock/<device_token>/…`, behind
`ADMS_RECEIVER`), the providers hr's registries were waiting for (device mappings, estate reconciliation, dependency
counters, the office summary section), the eSSL import (§7.5) and the standalone office agent `essl-agent/` ported to the
new protocol with the hardening of PLAN §6 (weeks 7–9). Deviations: DV-62 … DV-68 in `docs/DEVIATIONS.md`.

## What exists

| Area | Where | Notes |
|---|---|---|
| Models | `devices/models/{agent,device,logs,adms}.py` | `devices_agent` (service credential FK, intervals with DB range checks, `agent_version` DV-62), `devices_device` (every §2.9 column + `protocol`, `adms_server/adms_port`, `users_read_requested_at` DV-63; partial uniques on `serial_number`, `expected_serial`, `adms_token_hash`; `adms_enabled ⇒ token`; a device must be locatable: address, pin or serial; MAC format `aa:bb:…`), `devices_device_user` (PU `(device, pin)`), `devices_sync_log` *(no base)*, `devices_protocol_mapping` (PU scope), `devices_adms_request` *(no base, unmanaged, raw SQL: monthly range partitions + DEFAULT partition, body ≤ 1 MiB CHECK, DV-64)*, `devices_adms_unknown_device` (`last_reason`, DV-65). No `is_online` column anywhere. |
| Health | `devices/services/health.py` | the one place that decides ONLINE / DEGRADED / OFFLINE / NEVER_SEEN / IDENTITY_MISMATCH for terminals, agents (REVOKED) and offices (NO_AGENT, AWAITING_DEVICE), from timestamps only (`DEVICES_ONLINE_SECONDS` 300, `DEVICES_OFFLINE_SECONDS` 900; a slow agent widens its devices' window to its `offline_after_seconds`). A pushing terminal is judged on its pushes only. |
| Roster | `devices/services/roster.py` | presence watermark = the latest SUCCESS USERS sync log (`users_present`, `users_seen_at`) per device (2 queries for any number of devices); `device_state` × `software_state` per mapping; the four operator categories; per-employee presence. |
| Agents | `devices/services/agents.py` | create (credential issued, token shown once, one-time config download), rotate (a revoked agent gets a fresh credential), revoke, update (interval checks), delete (409 `agent_in_use`); `agent.ini` rendering. |
| Devices | `devices/services/devices.py` | create/update (validated; agent and serial are not editable — `rehome/` and the terminal itself decide), delete (409 `device_has_history`), `rehome` (`devices.manage`, reason audited), ADMS enable (per-device token, sha256 stored, allow-list) / disable, `refresh-employees` (a read request for the agent), reconciliation views, the mapping report. |
| Device users | `devices/services/device_users.py` | link **this row only**, auto-link (PIN = employee code, never overwrites), resolve (`confirm` required; CREATE_EMPLOYEE needs `employees.create`), map-pin (names the device, creates the row), unmapped (from the punch store). Every link change → `hr.attendance_inputs_changed` (`device_link_changed`). |
| Agent protocol | `devices/services/agent_protocol.py`, `devices/views/agent_api.py`, `devices/authentication.py` | config, heartbeat (truthful `reachable` per device, clock offset), announce (binds a label-registered device by its pin, never re-homes: 409 `device_bound_elsewhere` / `identity_mismatch` / `device_inactive`), identity-mismatch, discovery (locates by serial, a MAC objection is a mismatch), sync users (whole table = watermark), sync attendance (≤ 200, `Idempotency-Key`), sync-status. |
| Ingestion | `devices/services/ingest.py`, `devices/services/punch_sink.py` | one pipeline for both transports: validation, naive device time, content dedup key (A11), the registered punch sink (DV-66), one ATTENDANCE sync log, counters, `attendance.punches_ingested` for new punches only. |
| ADMS receiver | `devices/services/adms.py`, `devices/views/iclock.py` | evidence row for every request (1 MiB cap, credentials headers dropped, terminal-user passwords/templates redacted, ATTPHOTO/BIODATA never stored), serial **and** token, admission (active, enabled, allow-list on the trusted client IP), handshake `TransFlag=AttLog OpLog` without `TimeZone`, ATTLOG via the shared ingestion, USERINFO/OPERLOG `USER` lines → device users, per-token throttle (`iclock` 300/min → `ERROR`), always HTTP 200 `text/plain; charset=utf-8` with `Content-Length`. |
| Evidence upkeep | `devices/services/adms_evidence.py`, `devices/tasks.py`, `manage.py maintain_adms_evidence` | monthly partitions (stranded DEFAULT rows moved in), 30-day purge (whole partitions dropped, older rows deleted); daily Beat task; `deploy/release.sh` runs the command as the owner. |
| Providers | `devices/services/providers.py` (installed by `DevicesConfig.ready`) | hr `device_mappings`, `device_reconciler` (`devices/services/reconcile.py`), `employee_dependencies["devices"]`, `office_dependencies["devices"]`, `office_summary["devices"]` (only for callers with `devices.view`); dashboard counters (`devices` module); weekly ops report section (agent offline hours, unknown ADMS serials, identity mismatches). |
| Import | `devices/services/legacy_import.py` | see "eSSL import" below. |
| Office agent | `essl-agent/` | standalone package (stdlib + `pyzk`), own pytest suite (`essl-agent/tests`, pyzk and the platform replaced by doubles), `agent.ini.example`, `pyproject.toml`. |
| Fixtures | `devices/tests/legacy/` | `capture_essl.py` (how they were made), `essl_devices_tables.json` (masked `SELECT *` of 12 eSSL tables), `essl_devices_api.json` (masked eSSL API responses), `essl_adms_exchanges.json` (11 terminal requests with eSSL's exact replies). |

## Endpoints

Staff (`/api/v1/`, module `devices`):

| Path | Permission |
|---|---|
| `devices/` list (`office`, `agent`, `unassigned`, `is_active`, `adms_enabled`, `identity_status`, `protocol`, `search`, `ordering`) / detail · create · `PATCH` · `DELETE` (409 `device_has_history`) | view · create · edit · manage |
| `GET devices/mapping/`, `GET devices/<uid>/logs/` (cursor), `…/employee-reconciliation/`, `…/user-reconciliation/` | view |
| `POST devices/<uid>/refresh-employees/` (409 `device_inactive`) | sync |
| `POST devices/<uid>/rehome/`, `…/adms/enable/` (token shown once), `…/adms/disable/` | manage |
| `devices/device-users/` list (`device`, `linked`, `device_state`, `software_state`, `active_only`, `search`) / detail, `GET …/unmapped/?device=` | view |
| `POST devices/device-users/<uid>/link/`, `…/resolve/`, `devices/device-users/auto-link/?device=`, `devices/device-users/map-pin/` | edit |
| `devices/agents/` list (`office` = filed there or serving a device there) / detail, `GET …/logs/` | view |
| `PATCH devices/agents/<uid>/` | edit |
| `POST devices/agents/` (a credential is issued), `…/rotate-token/`, `…/revoke/`, `GET …/config-download/?download=`, `DELETE` (409 `agent_in_use`) | manage (credentials) |
| `devices/protocol-mappings/` CRUD (409 `protocol_mapping_exists`) + `GET …/observed/` | view · create · edit · manage (DELETE) |
| `GET devices/adms/status/`, `GET devices/adms/requests/?serial=&kind=&device=` (cursor, newest first), `GET devices/adms/unknown-devices/?reason=` | view |

The registry has no record scope for `devices` (PLAN §3.2), so there is no scope filtering to test; HR holds
`view`/`sync`, Admin `manage`. Every PATCH/action takes `expected_version` (409 `stale_version`). Telemetry written by
agents and terminals (`last_seen_at`, counters, `adms_*`) is written with `.update()` and never bumps `version`: a
machine report never makes a person's edit stale.

Agent (`/api/agent/v1/`, `Authorization: Bearer fl_<prefix>_<secret>`, throttle `agent`): `GET config/`,
`POST heartbeat/`, `POST devices/announce/`, `POST devices/identity-mismatch/`, `POST devices/discovery/`,
`POST sync/users/`, `POST sync/attendance/`, `GET sync-status/`. A staff JWT is not an agent token (401) and a
disabled agent's valid token is refused (401).

Terminals: `/iclock/<device_token>/{cdata,getrequest,devicecmd,registry,ping}[.aspx][/]` and a catch-all that is
recorded; 404 while `ADMS_RECEIVER` is off.

## Decisions not spelled out in the PLAN

1. **Identity is the serial, never the address.** A device is found by its uid or by the serial it reports (the pin
   while it has none). `announce` binds a label-registered device of the agent's own office by its pin and marks it
   VERIFIED; it never moves a device between agents or offices (`rehome/` is the only way, audited with a reason); a
   serial against the pin or a MAC against the known one is an IDENTITY_MISMATCH that nothing adopts; the same address
   answering with another serial is another device.
2. **Truthful reachability.** Heartbeat reports carry `reachable` per terminal; only `reachable: true` moves the
   device's `last_seen_at`. Uploads move `last_sync_at`/`last_punch_at`, not `last_seen_at`. A dead agent therefore
   ages its devices to OFFLINE (the eSSL sticky `is_online`, spec §I.19, is gone).
3. **The server never dials a terminal.** `refresh-employees/` records `users_read_requested_at`; the agent sees it in
   `config/` and uploads the table on its next cycle. The answer is always the last successful read (with a note);
   pushing terminals send user changes by themselves; an unassigned terminal cannot be read (note says: rehome it).
4. **Presence watermark.** Only a whole-table read (`sync/users/`) is a watermark; ADMS USERINFO/OPERLOG pushes update
   rows but are not (a push is not a table). A failed read after a good one keeps the good watermark and says
   `LAST_FAILED`. Nothing is ever deleted from a terminal or here (`device_deletion_supported: false`).
5. **Punch sink** (DV-66). Devices validates, dedups by content and announces; storing is the attendance package's.
   Until it registers a sink, uploads are accepted and reported as `discarded` (never as `new`), and no event is
   emitted. Cross-transport dedup: the same punch read by the agent and pushed over ADMS has one key.
6. **Config download.** `create`/`rotate-token` return the token once plus a one-time `download` id; the token is held
   Fernet-encrypted in the cache for `DEVICES_AGENT_CONFIG_TTL_SECONDS` (600) under the id's sha256 and handed out once
   as `agent.ini` (`Cache-Control: private, no-store`); a second fetch is 410 `download_expired`.
7. **ADMS replies.** Handshake: the option block without `TimeZone` and with `TransFlag=AttLog OpLog`; ATTLOG:
   `OK: <records parsed>` (eSSL's count); anything refused or quarantined: `OK` (an unknown terminal must not retry
   forever, and it gets no option block — eSSL handed every serial one, spec §I.21); a push whose ingestion failed
   inside this server: `ERROR` so the terminal resends (eSSL acknowledged and kept the punches as evidence only,
   §I.23); a request that cannot even be recorded: `ERROR` and a minimal `UNSTORABLE` row.
8. **Evidence partitions.** Monthly on `received_at` (UTC) with a DEFAULT partition; `ensure_partitions` moves stranded
   DEFAULT rows into a new month atomically. Partition upkeep needs the table owner: the release script runs
   `maintain_adms_evidence --months 3`, the daily task does it only when it may and always purges rows. Missing
   upcoming partitions are shown in `adms/status/` (a `/healthz` check was not added: core pins the set of checks).
9. **Estate reconciliation** (`hr/employees/reconcile-devices/`). Buckets `added` / `updated` / `unchanged` /
   `unknown` from rows present on their terminal's last read, `removed` / `unconfirmed` / `unlinked` from each active
   employee's presence. `apply` links, creates (needs `employees.create`) and deactivates (needs `employees.archive`,
   hr's own guards) exactly what the plan names; what the caller may not do is listed in `skipped`. `read_devices`
   asks each agent to re-read (it cannot dial).
10. **Throttles.** `iclock` is applied per device token inside the view (the receiver is not a DRF view); beyond it the
    terminal gets `ERROR` and nothing is recorded.

## The office agent (`essl-agent/`)

Port of the eSSL agent to `/api/agent/v1/` (platform tokens `fl_…`; an `essl_…` token is refused at start-up with the
instruction to download a new `agent.ini`; field names `pin`/`device_time`; device uid from `config/`/announce;
`Idempotency-Key` on uploads). Hardening, each covered by `essl-agent/tests`:

| eSSL behaviour | Now |
|---|---|
| announced every terminal on every 2-second delivery loop | once per process start, then at most every `announce_interval` (from `config/`, 3600 s); a refusal holds that terminal's queue until the next announce |
| uploaded the whole user table on every cycle | only when its sha256 changed, when the platform asked (`users_read_requested_at`), or at least every 24 h |
| re-enqueued every punch the terminal holds, relying on the unique index, which `purge_sent` (keep last 5000) defeated | read cursor (highest record uid, newest device time); numbering restart → by time with a 24 h overlap |
| delivery loop talked to the server even with nothing queued | no request at all when there is nothing to send |
| kept the last 5000 delivered rows | delivered rows purged after 7 days |
| heartbeat `is_online` = TCP port open (any hardware at the address) | `reachable` = port open now **and** the terminal answered as itself on its last read (within 2 sync intervals) |
| identity block in memory (a restart delivered the held queue) | persisted in the queue database, cleared only when the right terminal answers |
| eSSL queue file | migrated in place on open (`device_user_id`→`pin`, `punch_time`→`device_time`), nothing lost |

The read-only contract of `zk_reader` is unchanged (tests assert every terminal call is in `READ_CALLS`); pyzk is
imported lazily so the package and its tests run without it. `devices/tests/test_agent_e2e.py` runs the real agent
package against this server (`live_server`) with a simulated terminal: adopt from `config/` → read → announce (binds
and verifies the label-registered device) → users → punches (stored through the sink) → heartbeat (last seen, clock
offset) → nothing to say → an impostor at the same address is reported and nothing of it is stored.

## eSSL import (`devices.services.legacy_import`)

`hr.services.legacy_import.import_all` runs first (offices, employees); then `devices...import_all(tables)` —
idempotent through `core_legacy_map` (`ESSL` × table × id), each function returns `{created, updated, skipped,
violations}` and writes one `devices.legacy_imported` audit row.

| eSSL table | Platform | Notes |
|---|---|---|
| `agents` | `devices_agent` | `version`→`agent_version`; a **new** service credential per active agent, tokens returned once under `credentials` (the bcrypt `token_hash` is dropped); revoked/inactive → imported disabled without credential (violation); intervals outside the DB ranges repaired (violation). |
| `devices` | `devices_device` | `is_online` dropped (derived); `protocol` ADMS_PUSH → ZK_TCP; `adms_enabled` imported **off** (DV-68); MACs normalised; invalid address/port/timeout repaired; `serial_verified_at` → `identity_checked_at`; `last_punch_at` (naive) read in the office zone; a row with neither address nor serial is skipped. |
| `device_users` | `devices_device_user` | `device_user_id` → `pin`; per-device links kept; PINs linked on more than one device reported (field `pin`) for HR to confirm. |
| `sync_logs` | `devices_sync_log` | only each device's latest USERS log and latest SUCCESS USERS log (the watermark); others skipped by design. |
| `protocol_mappings` | `devices_protocol_mapping` | 1:1 (`null` → blank). |
| `adms_unknown_devices` | `devices_adms_unknown_device` | reason `UNKNOWN_SERIAL` (the only one eSSL had). |
| `adms_requests` | `devices_adms_request` | the last 30 days only (§7.5); the receiver's redaction applied (eSSL stored terminal-user passwords in bodies and parsed rows); immutable. |
| `attendance_raw`, `attendance` | — | the attendance package's import. |

## Parity evidence

* **Terminal conformance** — `devices/tests/test_iclock_conformance.py` replays the 11 requests recorded from a
  private eSSL receiver (`essl_adms_exchanges.json`): status, content type, body and `Content-Length` are byte-identical
  except three documented replies (the handshake's `TransFlag`; the unknown serial's and the serial-less handshakes
  answered `OK` instead of an option block). The replay leaves one evidence row per request, the two ATTLOG punches
  once (the resend is a duplicate), the OPERLOG user, and one quarantine entry.
* **API parity** — `devices/tests/test_legacy_parity.py` imports the masked eSSL tables and compares this API with the
  eSSL API responses captured right after the same scenario: devices (identity, addresses, office, agent, counters,
  awaiting discovery, mapping consistency, transport — PROJECT-01 is UNASSIGNED because ADMS imports off), agents
  (office, devices, status incl. REVOKED), every device user (device_state, software_state, active user, removal,
  sync state, employee, name, privilege, card, group), MARS-01/SALES-01 user and employee reconciliations (counters,
  categories, actions, suggestions), every employee's device mappings and presence counters, protocol mappings and
  the mapping report. Time-derived health is judged now, not at capture time, and is not compared.
* **How the fixtures were made** — a private copy of the eSSL database (`essl_wp_devices`, migrated and seeded by
  eSSL's own alembic chain and `scripts/seed.py`) and a private eSSL server on 127.0.0.1:18152 were driven only
  through eSSL's own staff API, agent protocol and ADMS receiver (`capture_essl.py`): three offices, three agents (one
  revoked), MARS-01 registered by address and verified, SALES-01 registered from its label and located by a LAN
  discovery, PROJECT-01 pushing over ADMS (serial adopted at the handshake), MARS-02 an identity mismatch, an inactive
  UDP terminal, `map-pin` linking PIN 2 on both terminals (the A1 case), a second users read dropping PIN 5, an ATTLOG
  push with a malformed line and its resend, OPERLOG USER lines, an unknown serial. Names are masked
  deterministically (`Person <sha256[:6]>`), e-mails replaced, phones, login password hashes and agent token hashes dropped; the terminal payloads of
  `essl_adms_exchanges.json` keep the capture's made-up names/passwords because they must stay byte-exact.
  `legacy_goldenapp` / `legacy_blog_cms` were never touched.

## Shared changes

* `flarize/settings/base.py`: `SPECTACULAR_SETTINGS["ENUM_NAME_OVERRIDES"]` += `DeviceHealthStatusEnum`,
  `DeviceAgentStatusEnum`, `DeviceSyncLogStatusEnum` (distinct enum names for three `status` fields);
  `CELERY_BEAT_SCHEDULE["devices.purge_adms_evidence"]` (daily 03:41).
* `deploy/release.sh`: `manage.py maintain_adms_evidence --months 3` after `ensure_audit_partitions` (owner role).
* `core/tests/test_app_layout.py`: `devices.urls.iclock_urlpatterns` is now a non-empty list.
* `hr/tests/test_employees_api.py`: the two "until the devices package provides them" tests set the providers to
  `None` for their duration (monkeypatch restores devices' providers); the dependency-count test expects the
  `device_mappings` counter devices now contributes.

## Open issues

* D-10 (ADMS controlled test on one terminal) is still open: `ADMS_RECEIVER` stays off; the ATTLOG column mapping
  (verify→status, status→punch) is marked UNPROVEN in every stored payload.
* The attendance package must register its punch sink (`devices.services.punch_sink.register`) and consume
  `attendance.punches_ingested`; until then punches are received and discarded (reported as such).
* Windows service / PyInstaller packaging of `essl-agent` (PLAN §5.7) is not part of this package.
