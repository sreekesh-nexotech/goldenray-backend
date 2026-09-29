"""Agent configuration: an ini file (as downloaded from Studio: ``agent.ini``), overridable by environment variables.

Nothing secret is ever written to the repository. The token is a platform service credential (``fl_<prefix>_<secret>``);
an eSSL token (``essl_…``) is refused at start-up with the instruction to download a new ``agent.ini``.
"""

from __future__ import annotations

import configparser
import os
from dataclasses import dataclass, field
from pathlib import Path

MAX_BATCH = 200  # the platform refuses attendance batches above 200 punches
TOKEN_PREFIX = "fl_"


class ConfigError(SystemExit):
    """The configuration cannot work; the message says what to fix."""


@dataclass
class DeviceConfig:
    """A terminal on the office LAN this agent reads.

    ``expected_serial`` is the pin: only hardware answering with that serial is read. ``expected_mac`` is a second,
    optional objection. ``uid`` is the platform's device uid once known (from the central configuration or an
    announce). ``identity_blocked`` is persisted in the queue database, so a restart never forgets a mismatch.
    """

    name: str
    ip: str
    port: int = 4370
    password: int = 0
    timeout: int = 15
    force_udp: bool = False
    expected_serial: str | None = None
    expected_mac: str | None = None
    serial: str | None = None
    uid: str | None = None
    from_server: bool = False
    identity_blocked: bool = False
    identity_error: str | None = None


@dataclass
class AgentConfig:
    server_url: str
    token: str
    api_version: str = "v1"
    agent_code: str = ""
    queue_path: str = "agent_queue.sqlite3"

    heartbeat_interval: int = 60
    sync_interval: int = 300
    upload_batch_size: int = MAX_BATCH
    # Announce each terminal at start-up and then at most this often (the platform says how often in /config/).
    announce_interval: int = 3600
    # The user table is uploaded when it changed, when the platform asked for a read, or at least this often (it is
    # the presence watermark: its date says how fresh "still enrolled" is).
    users_refresh_hours: int = 24
    # Delivered punches stay in the local queue this long for troubleshooting, then they are purged.
    sent_retention_days: int = 7
    # When a terminal's record numbering restarts (log cleared, replaced board), punches are taken by time from the
    # newest delivered one minus this overlap; the platform deduplicates by content.
    cursor_overlap_hours: int = 24

    retry_base_seconds: int = 15
    retry_max_seconds: int = 900
    request_timeout: int = 60
    verify_tls: bool = True

    devices: list[DeviceConfig] = field(default_factory=list)

    @property
    def api_base(self) -> str:
        return f"{self.server_url}/api/agent/{self.api_version}"

    @classmethod
    def load(cls, path: str | Path) -> "AgentConfig":
        path = Path(path)
        parser = configparser.ConfigParser()
        if path.exists():
            parser.read(path, encoding="utf-8")

        def get(key: str, default=None):
            env = os.environ.get(f"ESSL_AGENT_{key.upper()}")
            if env:
                return env
            if parser.has_option("agent", key):
                return parser.get("agent", key)
            return default

        def number(key: str, default: int, low: int, high: int) -> int:
            try:
                value = int(get(key, default))
            except (TypeError, ValueError):
                raise ConfigError(f"{key} must be a whole number (in {path}).")
            return max(low, min(high, value))

        server_url, token = str(get("server_url", "") or "").strip(), str(get("token", "") or "").strip()
        if not server_url or not token:
            raise ConfigError(f"server_url and token are required. Set them in {path} or via ESSL_AGENT_SERVER_URL / ESSL_AGENT_TOKEN.")
        if not token.startswith(TOKEN_PREFIX):
            raise ConfigError("This token is not a Flarize agent credential (eSSL tokens are not accepted). Download a new agent.ini from Studio → Devices → Agents.")

        cfg = cls(
            server_url=server_url.rstrip("/"),
            token=token,
            api_version=str(get("api_version", "v1") or "v1").strip(),
            agent_code=str(get("agent_code", "") or ""),
            queue_path=str(get("queue_path", "agent_queue.sqlite3")),
            heartbeat_interval=number("heartbeat_interval", 60, 10, 3600),
            sync_interval=number("sync_interval", 300, 30, 86400),
            upload_batch_size=number("upload_batch_size", MAX_BATCH, 1, MAX_BATCH),
            announce_interval=number("announce_interval", 3600, 60, 86400),
            users_refresh_hours=number("users_refresh_hours", 24, 1, 24 * 30),
            sent_retention_days=number("sent_retention_days", 7, 1, 365),
            cursor_overlap_hours=number("cursor_overlap_hours", 24, 1, 24 * 30),
            retry_base_seconds=number("retry_base_seconds", 15, 1, 3600),
            retry_max_seconds=number("retry_max_seconds", 900, 1, 86400),
            request_timeout=number("request_timeout", 60, 5, 600),
            verify_tls=str(get("verify_tls", "true")).lower() not in ("0", "false", "no"),
        )
        for section in parser.sections():
            if not section.startswith("device"):
                continue
            cfg.devices.append(
                DeviceConfig(
                    name=parser.get(section, "name", fallback=section),
                    ip=parser.get(section, "ip"),
                    port=parser.getint(section, "port", fallback=4370),
                    password=parser.getint(section, "password", fallback=0),
                    timeout=parser.getint(section, "timeout", fallback=15),
                    force_udp=parser.getboolean(section, "force_udp", fallback=False),
                    expected_serial=(parser.get(section, "expected_serial", fallback="") or "").strip() or None,
                    expected_mac=(parser.get(section, "expected_mac", fallback="") or "").strip() or None,
                )
            )
        return cfg
