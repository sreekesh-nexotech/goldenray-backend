"""Durable local queue of one office agent (SQLite, WAL, synchronous=FULL).

Everything read but not yet delivered lives on disk: an outage, a killed process or a reboot mid-upload loses
nothing, and nothing is retired until the platform confirmed it (stored, or already known). Delivered rows are kept
``sent_retention_days`` (7) for troubleshooting and then purged. Also kept here: the per-terminal read cursor (the
highest record number and the newest punch time read), the hash of the last delivered user table, identity blocks
(so a restart never forgets that the wrong hardware answered) and a short event log.

A queue file written by the eSSL agent (columns ``device_user_id``/``punch_time``) is migrated in place on open: no
queued punch is lost by upgrading.
"""

from __future__ import annotations

import json
import sqlite3
import threading
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Iterable

SCHEMA = """
CREATE TABLE IF NOT EXISTS meta (key TEXT PRIMARY KEY, value TEXT);

CREATE TABLE IF NOT EXISTS devices (
    serial       TEXT PRIMARY KEY,
    uid          TEXT,
    name         TEXT,
    ip           TEXT,
    port         INTEGER,
    firmware     TEXT,
    platform     TEXT,
    last_seen_at TEXT,
    last_error   TEXT
);

-- Unsent punches. The unique index is the agent-side half of deduplication: re-reading a terminal cannot enqueue
-- the same record twice (the platform deduplicates by content as well).
CREATE TABLE IF NOT EXISTS queue (
    id                INTEGER PRIMARY KEY AUTOINCREMENT,
    device_serial     TEXT NOT NULL,
    device_record_uid INTEGER,
    pin               TEXT NOT NULL,
    device_time       TEXT NOT NULL,
    status            INTEGER,
    punch             INTEGER,
    raw_payload       TEXT NOT NULL DEFAULT '{}',
    enqueued_at       TEXT NOT NULL,
    attempts          INTEGER NOT NULL DEFAULT 0,
    last_attempt_at   TEXT,
    last_error        TEXT,
    sent_at           TEXT
);
CREATE INDEX IF NOT EXISTS ix_queue_pending ON queue (sent_at, id);

CREATE TABLE IF NOT EXISTS sync_state (
    device_serial   TEXT PRIMARY KEY,
    last_record_uid INTEGER,
    last_sync_at    TEXT,
    last_upload_at  TEXT
);

CREATE TABLE IF NOT EXISTS events (id INTEGER PRIMARY KEY AUTOINCREMENT, at TEXT NOT NULL, level TEXT NOT NULL, message TEXT NOT NULL);
"""
QUEUE_IDENTITY = "CREATE UNIQUE INDEX IF NOT EXISTS ux_queue_identity ON queue (device_serial, device_record_uid, pin, device_time)"
SYNC_STATE_COLUMNS = {"last_device_time": "TEXT", "users_hash": "TEXT", "users_read_at": "TEXT", "users_uploaded_at": "TEXT"}
SYNC_STATE_FIELDS = ("last_record_uid", "last_device_time", "last_sync_at", "last_upload_at", "users_hash", "users_read_at", "users_uploaded_at")


def utcnow() -> datetime:
    return datetime.now(timezone.utc)


def _now() -> str:
    return utcnow().isoformat()


class Store:
    def __init__(self, path: str | Path):
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._lock = threading.Lock()
        self._conn = sqlite3.connect(str(self.path), check_same_thread=False)
        self._conn.row_factory = sqlite3.Row
        self._conn.execute("PRAGMA journal_mode=WAL")  # survives an abrupt kill without losing committed rows
        self._conn.execute("PRAGMA synchronous=FULL")
        self._conn.executescript(SCHEMA)
        self._migrate()
        self._conn.commit()

    def _columns(self, table: str) -> set[str]:
        return {row["name"] for row in self._conn.execute(f"PRAGMA table_info({table})")}

    def _migrate(self) -> None:
        """Bring an eSSL-agent queue file (or an older one of ours) to this schema without losing a row."""
        queue = self._columns("queue")
        if "device_user_id" in queue:
            self._conn.execute("DROP INDEX IF EXISTS ux_queue_identity")
            self._conn.execute("ALTER TABLE queue RENAME COLUMN device_user_id TO pin")
            self._conn.execute("ALTER TABLE queue RENAME COLUMN punch_time TO device_time")
        self._conn.execute(QUEUE_IDENTITY)
        devices = self._columns("devices")
        if "central_id" in devices and "uid" not in devices:
            self._conn.execute("ALTER TABLE devices ADD COLUMN uid TEXT")  # eSSL integer ids mean nothing here
        existing = self._columns("sync_state")
        for name, kind in SYNC_STATE_COLUMNS.items():
            if name not in existing:
                self._conn.execute(f"ALTER TABLE sync_state ADD COLUMN {name} {kind}")

    def close(self) -> None:
        with self._lock:
            self._conn.close()

    # -- meta ------------------------------------------------------------------------------------------------------
    def set_meta(self, key: str, value: Any) -> None:
        with self._lock:
            self._conn.execute("INSERT INTO meta(key, value) VALUES(?, ?) ON CONFLICT(key) DO UPDATE SET value=excluded.value", (key, json.dumps(value, default=str)))
            self._conn.commit()

    def get_meta(self, key: str, default=None):
        with self._lock:
            row = self._conn.execute("SELECT value FROM meta WHERE key=?", (key,)).fetchone()
        return json.loads(row["value"]) if row else default

    def delete_meta(self, key: str) -> None:
        with self._lock:
            self._conn.execute("DELETE FROM meta WHERE key=?", (key,))
            self._conn.commit()

    # -- identity blocks (persisted: a restart never delivers a held queue) ------------------------------------------
    def set_identity_block(self, address: str, message: str) -> None:
        self.set_meta(f"identity_block:{address}", {"message": message, "at": _now()})

    def identity_block(self, address: str) -> dict | None:
        return self.get_meta(f"identity_block:{address}")

    def clear_identity_block(self, address: str) -> None:
        self.delete_meta(f"identity_block:{address}")

    # -- devices ---------------------------------------------------------------------------------------------------
    def upsert_device(self, serial: str, **fields: Any) -> None:
        columns = {key: value for key, value in fields.items() if value is not None}
        with self._lock:
            self._conn.execute("INSERT OR IGNORE INTO devices(serial) VALUES(?)", (serial,))
            if columns:
                sets = ", ".join(f"{key}=?" for key in columns)
                self._conn.execute(f"UPDATE devices SET {sets} WHERE serial=?", (*columns.values(), serial))
            self._conn.commit()

    def clear_device_error(self, serial: str) -> None:
        with self._lock:
            self._conn.execute("UPDATE devices SET last_error=NULL WHERE serial=?", (serial,))
            self._conn.commit()

    def get_device(self, serial: str) -> dict | None:
        with self._lock:
            row = self._conn.execute("SELECT * FROM devices WHERE serial=?", (serial,)).fetchone()
        return dict(row) if row else None

    # -- queue -----------------------------------------------------------------------------------------------------
    def enqueue_many(self, device_serial: str, records: Iterable[dict[str, Any]]) -> int:
        """Add punches, ignoring any already held. Returns how many were new."""
        added = 0
        with self._lock:
            for record in records:
                device_time = record.get("device_time")
                if isinstance(device_time, datetime):
                    device_time = device_time.replace(tzinfo=None).isoformat()
                cursor = self._conn.execute(
                    "INSERT OR IGNORE INTO queue (device_serial, device_record_uid, pin, device_time, status, punch, raw_payload, enqueued_at) VALUES (?,?,?,?,?,?,?,?)",
                    (
                        device_serial,
                        record.get("device_record_uid"),
                        str(record.get("pin") or ""),
                        device_time,
                        record.get("status"),
                        record.get("punch"),
                        json.dumps(record.get("raw_payload") or {}, ensure_ascii=False, default=str),
                        _now(),
                    ),
                )
                added += max(cursor.rowcount, 0)
            self._conn.commit()
        return added

    def pending(self, limit: int = 200, device_serial: str | None = None) -> list[dict]:
        sql, params = "SELECT * FROM queue WHERE sent_at IS NULL", []
        if device_serial:
            sql += " AND device_serial=?"
            params.append(device_serial)
        sql += " ORDER BY id LIMIT ?"
        params.append(limit)
        with self._lock:
            rows = self._conn.execute(sql, params).fetchall()
        out = []
        for row in rows:
            item = dict(row)
            item["raw_payload"] = json.loads(item["raw_payload"] or "{}")
            out.append(item)
        return out

    def pending_count(self, device_serial: str | None = None) -> int:
        sql, params = "SELECT COUNT(*) c FROM queue WHERE sent_at IS NULL", []
        if device_serial:
            sql += " AND device_serial=?"
            params.append(device_serial)
        with self._lock:
            return self._conn.execute(sql, params).fetchone()["c"]

    def mark_sent(self, ids: list[int]) -> None:
        """Confirmed by the platform (stored or already present): safe to retire."""
        if ids:
            with self._lock:
                self._conn.executemany("UPDATE queue SET sent_at=? WHERE id=?", [(_now(), row_id) for row_id in ids])
                self._conn.commit()

    def mark_failed(self, ids: list[int], error: str) -> None:
        """The rows stay queued; only the attempt is recorded."""
        if ids:
            with self._lock:
                self._conn.executemany("UPDATE queue SET attempts=attempts+1, last_attempt_at=?, last_error=? WHERE id=?", [(_now(), error[:500], row_id) for row_id in ids])
                self._conn.commit()

    def failed_count(self) -> int:
        with self._lock:
            return self._conn.execute("SELECT COUNT(*) c FROM queue WHERE sent_at IS NULL AND attempts > 0").fetchone()["c"]

    def purge_sent(self, retention_days: int = 7, *, now: datetime | None = None) -> int:
        """Delete delivered rows older than ``retention_days`` (undelivered rows are never touched)."""
        cutoff = ((now or utcnow()) - timedelta(days=retention_days)).isoformat()
        with self._lock:
            cursor = self._conn.execute("DELETE FROM queue WHERE sent_at IS NOT NULL AND sent_at < ?", (cutoff,))
            self._conn.commit()
            return cursor.rowcount

    # -- sync state (the read cursor and the user-table hash) ------------------------------------------------------
    def set_sync_state(self, device_serial: str, **fields: Any) -> None:
        unknown = set(fields) - set(SYNC_STATE_FIELDS)
        if unknown:
            raise ValueError(f"unknown sync_state fields: {sorted(unknown)}")
        columns = {key: value for key, value in fields.items() if value is not None}
        with self._lock:
            self._conn.execute("INSERT OR IGNORE INTO sync_state(device_serial) VALUES(?)", (device_serial,))
            if columns:
                sets = ", ".join(f"{key}=?" for key in columns)
                self._conn.execute(f"UPDATE sync_state SET {sets} WHERE device_serial=?", (*columns.values(), device_serial))
            self._conn.commit()

    def get_sync_state(self, device_serial: str) -> dict:
        with self._lock:
            row = self._conn.execute("SELECT * FROM sync_state WHERE device_serial=?", (device_serial,)).fetchone()
        return dict(row) if row else {}

    # -- events ----------------------------------------------------------------------------------------------------
    def log(self, level: str, message: str) -> None:
        with self._lock:
            self._conn.execute("INSERT INTO events(at, level, message) VALUES(?,?,?)", (_now(), level, message[:2000]))
            self._conn.execute("DELETE FROM events WHERE id <= (SELECT id FROM events ORDER BY id DESC LIMIT 1 OFFSET 2000)")
            self._conn.commit()

    def recent_events(self, limit: int = 30) -> list[dict]:
        with self._lock:
            rows = self._conn.execute("SELECT * FROM events ORDER BY id DESC LIMIT ?", (limit,)).fetchall()
        return [dict(row) for row in rows]

    def stats(self) -> dict:
        with self._lock:
            queue = self._conn.execute(
                "SELECT COUNT(*) total, SUM(CASE WHEN sent_at IS NULL THEN 1 ELSE 0 END) pending, SUM(CASE WHEN sent_at IS NOT NULL THEN 1 ELSE 0 END) sent, "
                "SUM(CASE WHEN sent_at IS NULL AND attempts > 0 THEN 1 ELSE 0 END) failing FROM queue"
            ).fetchone()
            devices = self._conn.execute("SELECT COUNT(*) c FROM devices").fetchone()["c"]
            blocks = self._conn.execute("SELECT COUNT(*) c FROM meta WHERE key LIKE 'identity_block:%'").fetchone()["c"]
        return {
            "queue_total": queue["total"] or 0,
            "queue_pending": queue["pending"] or 0,
            "queue_sent": queue["sent"] or 0,
            "queue_failing": queue["failing"] or 0,
            "devices": devices,
            "identity_blocks": blocks,
        }
