"""The office agent loop.

    terminal (LAN, ZK/TCP) -> agent -> durable SQLite queue -> HTTPS /api/agent/v1/ -> platform

Reading and delivering are separate: the agent keeps reading and queueing while the Internet is down, and delivery
drains the queue whenever the link is back. Before anything is read, the terminal must prove it is the device this
agent was configured for (its serial equals the pin; its MAC, where one is known, agrees). A terminal that fails is an
IDENTITY_MISMATCH: nothing is read from it, nothing queued for it is delivered, the mismatch is reported, and the block
is persisted — a restart does not forget it (the eSSL agent kept it in memory only).

Hardening over the eSSL agent:

* **announce** once at start-up and then at most every ``announce_interval`` (the platform says how often; eSSL
  announced every terminal on every 2-second delivery loop). A refusal (``device_bound_elsewhere``,
  ``identity_mismatch``, ``device_inactive``) holds that terminal's queue until the next announce;
* the **user table** is uploaded only when it changed (sha256 of the rows), when the platform asked for a read
  (``users_read_requested_at``), or at least every ``users_refresh_hours``;
* the **read cursor** (highest record number and newest punch time) means a punch is queued once even after the
  delivered rows are purged; when the terminal's numbering restarts, punches are taken by time with an overlap and
  the platform deduplicates by content;
* **delivery** makes no request while there is nothing to send; batches are at most 200 punches, each with a
  content-derived ``Idempotency-Key``;
* delivered rows are **purged after 7 days** (eSSL kept the last 5000, and re-read punches older than that were
  queued again);
* **heartbeat reachability is the truth**: a terminal is reported reachable only when it accepts a connection now and
  answered as itself on the last read (eSSL reported any host answering on the port, even the wrong hardware).
"""

from __future__ import annotations

import hashlib
import json
import platform
import socket
import time
from datetime import datetime, timedelta, timezone

from essl_agent import VERSION, discovery
from essl_agent.config import MAX_BATCH, AgentConfig, DeviceConfig
from essl_agent.identity import display_mac, macs_match, normalize_mac, normalize_serial
from essl_agent.store import Store, utcnow
from essl_agent.uploader import AuthRejected, Refused, ServerUnavailable, Uploader
from essl_agent.zk_reader import DeviceError, read_attendance, read_info, read_users, tcp_reachable

HOLDING_REFUSALS = {"device_bound_elsewhere", "identity_mismatch", "device_inactive"}


def local_ip() -> str | None:
    return discovery.local_ipv4()


def digest(*parts: str) -> str:
    return hashlib.sha256("|".join(parts).encode("utf-8")).hexdigest()[:32]


def parse_moment(value) -> datetime | None:
    """An aware datetime from the platform's ISO text (any offset, or ``Z``); ``None`` when absent or unreadable."""
    if not value:
        return None
    try:
        parsed = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    except ValueError:
        return None
    return parsed if parsed.tzinfo is not None else parsed.replace(tzinfo=timezone.utc)


def users_hash(users: list[dict]) -> str:
    """What the platform keeps of a user table, hashed (order-independent; raw payloads excluded)."""
    rows = sorted(({key: user.get(key) for key in ("pin", "device_uid", "name", "privilege", "card", "group_id", "has_password")} for user in users), key=lambda row: str(row["pin"]))
    return hashlib.sha256(json.dumps(rows, sort_keys=True, default=str).encode()).hexdigest()


class Agent:
    def __init__(self, cfg: AgentConfig, store: Store | None = None, uploader: Uploader | None = None, clock=time.monotonic):
        self.cfg = cfg
        self.store = store or Store(cfg.queue_path)
        self.uploader = uploader or Uploader(cfg.api_base, cfg.token, cfg.request_timeout, cfg.verify_tls)
        self.clock = clock
        self.last_error: str | None = None
        self._backoff = 0
        self._next_upload_at = 0.0
        self._announced_at: dict[str, float] = {}  # serial -> clock of the last accepted announce
        self._refused: dict[str, tuple[float, str]] = {}  # serial -> (clock, message) of an announce refusal
        self._read_requests: dict[str, str] = {}  # device uid -> users_read_requested_at from the platform
        self._verified: dict[str, dict] = {}  # address -> last read that answered as itself
        for dev in self.cfg.devices:
            self._restore_block(dev)

    # -- helpers ---------------------------------------------------------------------------------------------------
    def log(self, level: str, message: str) -> None:
        print(f"[{utcnow().strftime('%H:%M:%S')}] {level:5} {message}", flush=True)
        self.store.log(level, message)

    @staticmethod
    def address(dev: DeviceConfig) -> str:
        return f"{dev.ip}:{dev.port}"

    def _restore_block(self, dev: DeviceConfig) -> None:
        block = self.store.identity_block(self.address(dev))
        if block:
            dev.identity_blocked, dev.identity_error = True, block.get("message")

    def _pinned_serial(self, dev: DeviceConfig) -> str | None:
        """The configured pin wins; without one, the first serial read at this address is remembered and enforced."""
        if dev.expected_serial:
            return normalize_serial(dev.expected_serial)
        return normalize_serial(self.store.get_meta(f"serial:{self.address(dev)}"))

    def _pinned_mac(self, dev: DeviceConfig) -> str | None:
        if dev.expected_mac:
            return normalize_mac(dev.expected_mac)
        return normalize_mac(self.store.get_meta(f"mac:{self.address(dev)}"))

    def serial_of(self, dev: DeviceConfig) -> str | None:
        return dev.serial or self._pinned_serial(dev)

    # -- central configuration -------------------------------------------------------------------------------------
    def apply_config(self, config: dict | None) -> None:
        if not config:
            return
        self.adopt_server_devices(config)
        intervals = config.get("intervals") or {}
        if intervals.get("heartbeat_seconds"):
            self.cfg.heartbeat_interval = int(intervals["heartbeat_seconds"])
        if intervals.get("sync_seconds"):
            self.cfg.sync_interval = int(intervals["sync_seconds"])
        if config.get("announce_interval_seconds"):
            self.cfg.announce_interval = int(config["announce_interval_seconds"])
        if config.get("upload_batch_size"):
            self.cfg.upload_batch_size = max(1, min(MAX_BATCH, int(config["upload_batch_size"])))

    def adopt_server_devices(self, config: dict) -> int:
        """Take on the terminals the platform lists for this agent. The local file wins wherever the two disagree
        (it is the statement of whoever stood in the office); a pin set locally is never overwritten; a terminal
        without an address is left to ``discover``."""
        added = 0
        for entry in config.get("devices") or []:
            ip, port = entry.get("ip_address"), int(entry.get("port") or 4370)
            pin = normalize_serial(entry.get("expected_serial") or entry.get("serial_number"))
            if entry.get("uid") and entry.get("users_read_requested_at"):
                self._read_requests[str(entry["uid"])] = str(entry["users_read_requested_at"])
            existing = None
            for dev in self.cfg.devices:
                pinned = normalize_serial(dev.expected_serial or dev.serial)
                if pin and pinned == pin:
                    existing = dev
                    break
                # the address only while one side is unpinned: two pinned devices at one address are two devices
                if ip and dev.ip == ip and dev.port == port and not (pin and pinned):
                    existing = dev
                    break
            if existing is not None:
                existing.uid = str(entry.get("uid") or existing.uid or "") or None
                if not existing.expected_serial and pin:
                    existing.expected_serial = pin
                if not existing.expected_mac and entry.get("expected_mac"):
                    existing.expected_mac = entry["expected_mac"]
                continue
            if not ip:
                self.log("INFO", f"{entry.get('name')} [{pin or 'unpinned'}]: registered centrally, not located on this LAN yet - run: python -m essl_agent discover")
                continue
            dev = DeviceConfig(
                name=entry.get("name") or pin or f"{ip}:{port}",
                ip=ip,
                port=port,
                password=int(entry.get("comm_password") or 0),
                timeout=int(entry.get("timeout_seconds") or 15),
                force_udp=entry.get("protocol") == "ZK_UDP",
                expected_serial=pin,
                expected_mac=entry.get("expected_mac"),
                uid=str(entry["uid"]) if entry.get("uid") else None,
                from_server=True,
            )
            self._restore_block(dev)
            self.cfg.devices.append(dev)
            added += 1
            self.log("INFO", f"adopted {dev.name} at {ip}:{port} pinned to {pin or 'no serial'} from the central configuration")
        return added

    # -- discovery -------------------------------------------------------------------------------------------------
    def discover(self, subnet: str | None = None, prefix: int = 24, port: int = 4370, password: int = 0, report: bool = True, hosts=None) -> dict:
        result = discovery.discover(subnet=subnet, port=port, password=password, prefix=prefix, hosts=hosts, on_progress=lambda message: self.log("INFO", message))
        result["scanned_at"] = utcnow().isoformat()
        for host in result["found"]:
            if host.get("error"):
                self.log("WARN", f"{host['ip_address']}: answered on {port} but did not identify as a terminal: {host['error']}")
            else:
                self.log("INFO", f"{host['ip_address']}: serial {host.get('serial_number')} MAC {host.get('mac_address')} ({host.get('device_name')} / {host.get('platform')})")
        if not report:
            return {"scan": result, "server": None}
        try:
            server = self.uploader.report_discovery(result)
        except (AuthRejected, ServerUnavailable, Refused) as exc:
            self.log("WARN", f"discovery not reported centrally: {exc}")
            return {"scan": result, "server": None, "error": str(exc)}
        for match in server.get("matches", []):
            level = "INFO" if match.get("matched") else "ERROR"
            self.log(level, f"{'MATCHED' if match.get('matched') else 'IDENTITY PROBLEM'} {match.get('reported_serial')} at {match.get('ip_address')}: {match.get('message')}")
        for serial in server.get("still_awaiting", []):
            self.log("WARN", f"{serial}: not found on this LAN")
        return {"scan": result, "server": server}

    # -- identity --------------------------------------------------------------------------------------------------
    def _identity_fault(self, dev: DeviceConfig, info: dict) -> str | None:
        reported = normalize_serial(info.get("serial_number"))
        expected = self._pinned_serial(dev)
        if not reported:
            return "the terminal reported no serial number"
        if expected and reported != expected:
            return f"expected serial {expected}, terminal reported {reported}"
        expected_mac = self._pinned_mac(dev)
        if macs_match(expected_mac, info.get("mac_address")) is False:
            return f"expected MAC {display_mac(expected_mac)}, terminal reported {display_mac(info.get('mac_address'))}"
        return None

    def _block_identity(self, dev: DeviceConfig, info: dict, reason: str) -> None:
        """Refuse this terminal until the right one answers; its queue is held (it belongs to the real device)."""
        expected, reported = self._pinned_serial(dev), normalize_serial(info.get("serial_number"))
        expected_mac, reported_mac = display_mac(self._pinned_mac(dev)), display_mac(info.get("mac_address"))
        observed = utcnow().isoformat()
        message = (
            f"IDENTITY_MISMATCH {dev.name} at {self.address(dev)}: {reason}. expected serial {expected or 'unpinned'}; reported serial {reported or 'none'}; "
            f"expected MAC {expected_mac or 'unpinned'}; reported MAC {reported_mac or 'none'}; observed {observed}"
        )
        dev.identity_blocked, dev.identity_error = True, message
        self.store.set_identity_block(self.address(dev), message)
        self._verified.pop(self.address(dev), None)
        self.log("ERROR", message)
        self.log("ERROR", f"{dev.name}: refusing to sync; {self.store.pending_count(expected) if expected else 0} queued punch(es) held, none uploaded.")
        if expected:
            self.store.upsert_device(expected, last_error=message)
        try:
            self.uploader.report_identity_mismatch(
                {
                    "device": dev.uid,
                    "expected_serial": expected,
                    "reported_serial": reported,
                    "expected_mac": expected_mac,
                    "reported_mac": reported_mac,
                    "ip_address": dev.ip,
                    "port": dev.port,
                    "reason": reason[:500],
                    "observed_at": observed,
                }
            )
        except (AuthRejected, ServerUnavailable, Refused) as exc:  # the block stands whether or not the platform heard
            self.log("WARN", f"{dev.name}: mismatch not reported centrally yet: {exc}")

    # -- device side -----------------------------------------------------------------------------------------------
    def read_device(self, dev: DeviceConfig) -> bool:
        """Read one terminal into the local queue (needs only the office LAN)."""
        if not tcp_reachable(dev.ip, dev.port, timeout=4):
            self._verified.pop(self.address(dev), None)
            self.log("WARN", f"{dev.name} ({self.address(dev)}) not reachable on the LAN")
            return False
        try:
            info = read_info(dev.ip, dev.port, dev.password, dev.timeout, dev.force_udp)
        except DeviceError as exc:
            self._verified.pop(self.address(dev), None)
            self.log("ERROR", f"{dev.name}: device info failed: {exc}")
            return False
        fault = self._identity_fault(dev, info)
        if fault is not None:
            self._block_identity(dev, info, fault)
            return False

        serial = normalize_serial(info.get("serial_number"))
        observed = utcnow()
        if dev.identity_blocked:
            self.log("INFO", f"{dev.name}: the expected terminal answers again ({serial}); the held queue will be delivered")
        dev.serial, dev.identity_blocked, dev.identity_error = serial, False, None
        self.store.clear_identity_block(self.address(dev))
        self.store.set_meta(f"serial:{self.address(dev)}", serial)
        if normalize_mac(info.get("mac_address")):
            self.store.set_meta(f"mac:{self.address(dev)}", normalize_mac(info.get("mac_address")))
        self.store.upsert_device(serial, name=dev.name, ip=dev.ip, port=dev.port, firmware=info.get("firmware_version"), platform=info.get("platform"), last_seen_at=observed.isoformat())
        self.store.clear_device_error(serial)
        self.store.set_meta(f"info:{serial}", info)
        self._verified[self.address(dev)] = {"at": self.clock(), "device_time": info.get("device_time"), "observed_at": observed.isoformat()}

        try:
            users = read_users(dev.ip, dev.port, dev.password, dev.timeout, dev.force_udp)
            self.store.set_meta(f"users:{serial}", {"users": users, "hash": users_hash(users), "read_at": observed.isoformat()})
        except DeviceError as exc:
            users = None
            self.log("WARN", f"{dev.name}: user read failed: {exc}")
        try:
            records = read_attendance(dev.ip, dev.port, dev.password, dev.timeout, dev.force_udp)
        except DeviceError as exc:
            self.log("ERROR", f"{dev.name}: attendance read failed: {exc}")
            return False
        fresh = self._after_cursor(serial, records)
        added = self.store.enqueue_many(serial, fresh)
        uids = [record["device_record_uid"] for record in records if isinstance(record.get("device_record_uid"), int)]
        times = [record["device_time"] for record in records if record.get("device_time")]
        self.store.set_sync_state(serial, last_record_uid=max(uids) if uids else None, last_device_time=max(times) if times else None, last_sync_at=observed.isoformat())
        self.log("INFO", f"{dev.name} [{serial}]: {len(records)} punches on the terminal, {len(fresh)} after the cursor, {added} new to the queue; {len(users) if users is not None else '?'} users")
        return True

    def _after_cursor(self, serial: str, records: list[dict]) -> list[dict]:
        """The records not read before: above the last record number, or — when the terminal's numbering restarted —
        newer than the newest punch read minus the overlap. The first read takes everything the terminal holds."""
        state = self.store.get_sync_state(serial)
        last_uid, last_time = state.get("last_record_uid"), state.get("last_device_time")
        if last_uid is None and not last_time:
            return list(records)
        uids = [record["device_record_uid"] for record in records if isinstance(record.get("device_record_uid"), int)]
        if last_uid is not None and uids and max(uids) >= last_uid:
            since = None
            if last_time:
                since = (datetime.fromisoformat(last_time) - timedelta(hours=self.cfg.cursor_overlap_hours)).isoformat()
            return [
                r
                for r in records
                if (isinstance(r.get("device_record_uid"), int) and r["device_record_uid"] > last_uid)
                or (not isinstance(r.get("device_record_uid"), int) and since and (r.get("device_time") or "") >= since)
            ]
        if not last_time:
            return list(records)
        if last_uid is not None and uids:
            self.log("WARN", f"{serial}: the terminal's record numbering restarted (highest {max(uids)} < {last_uid}); taking punches by time")
        since = (datetime.fromisoformat(last_time) - timedelta(hours=self.cfg.cursor_overlap_hours)).isoformat()
        return [record for record in records if (record.get("device_time") or "") >= since]

    # -- platform side ---------------------------------------------------------------------------------------------
    def _announce_due(self, serial: str) -> bool:
        at = self._announced_at.get(serial)
        return at is None or self.clock() - at >= self.cfg.announce_interval

    def _refusal(self, serial: str) -> str | None:
        refused = self._refused.get(serial)
        if refused is None:
            return None
        if self.clock() - refused[0] >= self.cfg.announce_interval:
            del self._refused[serial]
            return None
        return refused[1]

    def _users_due(self, dev: DeviceConfig, serial: str) -> dict | None:
        table = self.store.get_meta(f"users:{serial}")
        if not table:
            return None
        state = self.store.get_sync_state(serial)
        uploaded = state.get("users_uploaded_at")
        if state.get("users_hash") != table["hash"] or not uploaded:
            return table
        if (utcnow() - datetime.fromisoformat(uploaded)) >= timedelta(hours=self.cfg.users_refresh_hours):
            return table
        requested = parse_moment(self._read_requests.get(dev.uid or ""))
        if requested and requested > datetime.fromisoformat(uploaded) and datetime.fromisoformat(table["read_at"]) > requested:
            return table  # the platform asked for a read after the last upload, and this table was read after it asked
        return None

    def announce(self, dev: DeviceConfig, serial: str) -> bool:
        info = self.store.get_meta(f"info:{serial}") or {}
        verified = self._verified.get(self.address(dev)) or {}
        payload = {
            "serial_number": serial,
            "name": dev.name,
            "ip_address": dev.ip,
            "port": dev.port,
            "protocol": "ZK_UDP" if dev.force_udp else "ZK_TCP",
            "model": info.get("device_name") or "",
            "firmware_version": info.get("firmware_version") or "",
            "platform": info.get("platform") or "",
            "mac_address": display_mac(info.get("mac_address")),
            "device_time": info.get("device_time"),
            "observed_at": verified.get("observed_at"),
            "device_info": {key: value for key, value in info.items() if key != "network_params"},
        }
        try:
            answer = self.uploader.announce_device(payload)
        except Refused as exc:
            self._refused[serial] = (self.clock(), f"{exc.code}: {exc.message}")
            level = "ERROR" if exc.code in HOLDING_REFUSALS else "WARN"
            self.log(level, f"{dev.name} [{serial}]: the platform refused the announce ({exc.code}): {exc.message} - its queue is held")
            return False
        dev.uid = str(answer.get("device") or dev.uid or "") or None
        self.store.upsert_device(serial, uid=dev.uid)
        self._announced_at[serial] = self.clock()
        self._refused.pop(serial, None)
        return True

    def deliver(self) -> dict:
        """Push queued work: announce when due, the user table when due, then the punches in batches of ≤ 200."""
        summary = {"uploaded": 0, "duplicate": 0, "discarded": 0, "batches": 0, "users": 0, "announced": 0, "requests": 0}
        if self.clock() < self._next_upload_at:
            return summary
        failed = False
        for dev in self.cfg.devices:
            if dev.identity_blocked:
                continue  # wrong hardware answered at this address: its queue is held, never filed under it
            serial = self.serial_of(dev)
            if not serial:
                continue
            pending = self.store.pending_count(serial)
            users = self._users_due(dev, serial)
            # an announce is contact (the platform moves the terminal's last_seen_at and measures its clock from it): only
            # a terminal that answered as itself in this process is announced, never an identity remembered from an
            # earlier run while the terminal is down
            reached = self.address(dev) in self._verified
            if not pending and users is None and not (self._announce_due(serial) and reached):
                continue  # nothing to say: no request at all
            try:
                if reached and self._announce_due(serial) and self._refusal(serial) is None:
                    summary["requests"] += 1
                    if self.announce(dev, serial):
                        summary["announced"] += 1
                if self._refusal(serial) is not None:
                    continue
                target = dev.uid or (self.store.get_device(serial) or {}).get("uid")
                if users is not None:
                    key = f"usr-{digest(serial, users['hash'], users['read_at'])}"
                    summary["requests"] += 1
                    result = self.uploader.upload_users(target, serial, users["users"], read_at=users["read_at"], idempotency_key=key)
                    self.store.set_sync_state(serial, users_hash=users["hash"], users_read_at=users["read_at"], users_uploaded_at=utcnow().isoformat())
                    summary["users"] += int(result.get("received", 0))
                while True:
                    batch = self.store.pending(self.cfg.upload_batch_size, serial)
                    if not batch:
                        break
                    ids = [row["id"] for row in batch]
                    batch_id = digest(serial, ",".join(str(row_id) for row_id in ids))
                    records = [
                        {
                            "device_record_uid": row["device_record_uid"],
                            "pin": row["pin"],
                            "device_time": row["device_time"],
                            "status": row["status"],
                            "punch": row["punch"],
                            "raw_payload": row["raw_payload"],
                        }
                        for row in batch
                    ]
                    try:
                        summary["requests"] += 1
                        result = self.uploader.upload_attendance(target, serial, records, batch_id=batch_id, idempotency_key=f"att-{batch_id}")
                    except (AuthRejected, ServerUnavailable, Refused) as exc:
                        self.store.mark_failed(ids, str(exc))
                        raise
                    self.store.mark_sent(ids)  # accepted = stored or already known (or, without a punch store, received)
                    summary["uploaded"] += int(result.get("new", 0))
                    summary["duplicate"] += int(result.get("duplicate", 0))
                    summary["discarded"] += int(result.get("discarded", 0))
                    summary["batches"] += 1
                self.store.set_sync_state(serial, last_upload_at=utcnow().isoformat())
            except Refused as exc:
                if exc.code == "device_not_found":
                    self._announced_at.pop(serial, None)  # rehomed or deleted centrally: announce again next time
                    dev.uid = None
                self.log("ERROR", f"{dev.name} [{serial}]: the platform refused an upload ({exc.code}): {exc.message} - queue held")
                continue
            except AuthRejected as exc:
                self._fail(f"credential refused: {exc}")
                failed = True
                break
            except ServerUnavailable as exc:
                self._fail(f"server unavailable: {exc}")
                failed = True
                break
        if not failed:
            self._succeed()
        if summary["batches"]:
            self.log("INFO", f"delivered {summary['batches']} batch(es): {summary['uploaded']} new, {summary['duplicate']} already known; queue now {self.store.pending_count()}")
        purged = self.store.purge_sent(self.cfg.sent_retention_days)
        if purged:
            self.log("INFO", f"purged {purged} delivered punch(es) older than {self.cfg.sent_retention_days} days from the local queue")
        return summary

    def _fail(self, message: str) -> None:
        self.last_error = message
        self._backoff = min(self.cfg.retry_max_seconds, max(self.cfg.retry_base_seconds, self._backoff * 2 or self.cfg.retry_base_seconds))
        self._next_upload_at = self.clock() + self._backoff
        self.log("WARN", f"{message} - queue held ({self.store.pending_count()} pending), retry in {self._backoff}s")

    def _succeed(self) -> None:
        self.last_error = None
        self._backoff = 0
        self._next_upload_at = 0.0

    # -- heartbeat -------------------------------------------------------------------------------------------------
    def device_reports(self) -> list[dict]:
        """Each terminal's reachability, truthfully: it accepts a connection now AND answered as itself on its last
        read (within two sync intervals). A blocked terminal is never reachable."""
        window = 2 * self.cfg.sync_interval + 60
        reports = []
        for dev in self.cfg.devices:
            serial = self.serial_of(dev)
            if not (dev.uid or serial):
                continue
            verified = self._verified.get(self.address(dev))
            fresh = verified is not None and self.clock() - verified["at"] <= window
            reachable = bool(not dev.identity_blocked and fresh and tcp_reachable(dev.ip, dev.port, timeout=3))
            error = dev.identity_error or (None if reachable else f"{self.address(dev)} not reachable, or not verified on the last read")
            report = {"device": dev.uid, "serial_number": serial, "reachable": reachable, "last_error": error}
            if reachable:
                report.update(device_time=verified.get("device_time"), observed_at=verified.get("observed_at"))
            reports.append(report)
        return reports

    def heartbeat(self) -> dict | None:
        payload = {
            "version": VERSION,
            "hostname": socket.gethostname()[:120],
            "platform": platform.platform()[:120],
            "local_ip": local_ip(),
            "queued_records": self.store.pending_count(),
            "failed_uploads": self.store.failed_count(),
            "last_error": self.last_error,
            "devices": self.device_reports(),
        }
        try:
            config = self.uploader.heartbeat(payload)
        except AuthRejected as exc:
            self.log("ERROR", f"heartbeat rejected (download a new agent.ini if the token was rotated): {exc}")
            return None
        except (ServerUnavailable, Refused) as exc:
            self.log("WARN", f"heartbeat failed: {exc}")
            return None
        self.apply_config(config)
        return config

    # -- main loop -------------------------------------------------------------------------------------------------
    def run_once(self) -> dict:
        """One full cycle: read every terminal, then deliver what is queued."""
        for dev in list(self.cfg.devices):
            self.read_device(dev)
        return self.deliver()

    def run(self, *, sleep=time.sleep, cycles: int | None = None) -> int:
        self.log("INFO", f"{self.cfg.agent_code or 'office agent'} v{VERSION} starting; platform {self.cfg.api_base}; queue {self.store.path}")
        config = self.heartbeat()
        if config:
            office = ((config.get("agent") or {}).get("office") or {}).get("name")
            self.log("INFO", f"registered with the platform; office = {office or 'unassigned'}; {len(self.cfg.devices)} terminal(s)")
        else:
            self.log("WARN", "platform not reachable at start-up - reading terminals anyway, the queue drains later")
        last_sync = last_beat = self.clock()
        first = True
        try:
            while cycles is None or cycles > 0:
                now = self.clock()
                if not first and now - last_beat >= self.cfg.heartbeat_interval:
                    self.heartbeat()
                    last_beat = now
                if first or now - last_sync >= self.cfg.sync_interval:
                    for dev in list(self.cfg.devices):
                        self.read_device(dev)
                    last_sync = now
                first = False
                self.deliver()
                if cycles is not None:
                    cycles -= 1
                sleep(2)
        except KeyboardInterrupt:
            self.log("INFO", "stopping (the queue is on disk and resumes on the next start)")
        finally:
            self.store.close()
        return 0
