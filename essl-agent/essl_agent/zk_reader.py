"""Read-only ZK/TCP client (pyzk), verified against an eSSL AI FACE MARS (ZAM180_TFT, "Ver 6.60 Aug 19 2021").

Safety contract (do not weaken): only reads exist here. These pyzk calls are deliberately absent and must never be
added — ``delete_user``, ``set_user``, ``clear_attendance``, ``delete_attendance``, ``clear_data``, ``set_time``,
``disable_device``, ``enable_device``, ``restart``, ``poweroff``, ``unlock``, ``test_voice``, ``write_lcd``,
``clear_lcd``. ``disable_device`` is not used before bulk reads either: it would stop staff punching.

The terminal's user id is always a string (real terminals mix ``1``, ``55`` and ``EMP001``); a punch time is the
terminal's wall clock, naive, sent as it was read; ``status``/``punch`` are raw integers whose meaning is never
guessed here. ``pyzk`` is imported when a terminal is contacted, so the rest of the agent (and its tests) run
without it.
"""

from __future__ import annotations

import socket
from contextlib import contextmanager
from datetime import date, datetime
from typing import Any, Iterator

INFO_GETTERS = (
    ("device_name", "get_device_name"),
    ("serial_number", "get_serialnumber"),
    ("mac_address", "get_mac"),
    ("firmware_version", "get_firmware_version"),
    ("platform", "get_platform"),
    ("face_version", "get_face_version"),
    ("fp_version", "get_fp_version"),
    ("device_time", "get_time"),
    ("network_params", "get_network_params"),
)
READ_CALLS = frozenset({"connect", "disconnect", "get_users", "get_attendance", *(getter for _, getter in INFO_GETTERS)})


class DeviceError(RuntimeError):
    """The terminal could not be reached or refused the session."""


def _zk_class():
    from zk import ZK  # pyzk

    return ZK


def jsonable(value: Any) -> Any:
    if isinstance(value, datetime):
        return value.replace(tzinfo=None).isoformat() if value.tzinfo is None else value.isoformat()
    if isinstance(value, date):
        return value.isoformat()
    if isinstance(value, bytes):
        return value.decode("gb18030", errors="replace")
    if isinstance(value, (list, tuple)):
        return [jsonable(item) for item in value]
    if isinstance(value, dict):
        return {str(key): jsonable(item) for key, item in value.items()}
    if isinstance(value, (str, int, float, bool)) or value is None:
        return value
    if hasattr(value, "__dict__"):
        return {str(key): jsonable(item) for key, item in vars(value).items() if key not in ("password",)}
    return str(value)


@contextmanager
def connection(ip: str, port: int = 4370, password: int = 0, timeout: int = 15, force_udp: bool = False) -> Iterator[Any]:
    try:
        zk = _zk_class()(ip, port=port, timeout=timeout, password=password, force_udp=force_udp, ommit_ping=True)
        conn = zk.connect()
    except Exception as exc:  # noqa: BLE001 - every failure to open a session is a DeviceError
        raise DeviceError(f"{type(exc).__name__}: {exc}") from exc
    try:
        yield conn
    finally:
        try:
            conn.disconnect()
        except Exception:  # noqa: BLE001
            pass


def tcp_reachable(ip: str, port: int, timeout: float = 3.0) -> bool:
    try:
        with socket.create_connection((ip, port), timeout=timeout):
            return True
    except OSError:
        return False


def read_info(ip: str, port: int = 4370, password: int = 0, timeout: int = 15, force_udp: bool = False) -> dict[str, Any]:
    info: dict[str, Any] = {}
    unavailable: dict[str, str] = {}
    with connection(ip, port, password, timeout, force_udp) as conn:
        for key, getter in INFO_GETTERS:
            fn = getattr(conn, getter, None)
            if fn is None:
                unavailable[key] = "not supported by the client library"
                continue
            try:
                info[key] = jsonable(fn())
            except Exception as exc:  # noqa: BLE001 - one unreadable field is reported, not fatal
                unavailable[key] = f"{type(exc).__name__}: {exc}"
    info["unavailable"] = unavailable
    return info


def read_users(ip: str, port: int = 4370, password: int = 0, timeout: int = 15, force_udp: bool = False) -> list[dict[str, Any]]:
    """The terminal's whole user table, verbatim (the password itself is never read out, only whether one is set)."""
    with connection(ip, port, password, timeout, force_udp) as conn:
        users = conn.get_users()
    return [
        {
            "pin": str(getattr(user, "user_id", "") or ""),
            "device_uid": getattr(user, "uid", None),
            "name": (getattr(user, "name", "") or "").strip(),
            "privilege": getattr(user, "privilege", None),
            "card": str(getattr(user, "card", "") or ""),
            "group_id": str(getattr(user, "group_id", "") or ""),
            "has_password": bool(getattr(user, "password", "")),
            "raw_payload": jsonable(user),
        }
        for user in users
    ]


def read_attendance(ip: str, port: int = 4370, password: int = 0, timeout: int = 15, force_udp: bool = False) -> list[dict[str, Any]]:
    """Every attendance record the terminal holds, verbatim."""
    with connection(ip, port, password, timeout, force_udp) as conn:
        logs = conn.get_attendance()
    records = []
    for log in logs:
        stamp = getattr(log, "timestamp", None)
        records.append(
            {
                "device_record_uid": getattr(log, "uid", None),
                "pin": str(getattr(log, "user_id", "") or ""),
                "device_time": stamp.replace(tzinfo=None).isoformat() if isinstance(stamp, datetime) else (str(stamp) if stamp else None),
                "status": getattr(log, "status", None),
                "punch": getattr(log, "punch", None),
                "raw_payload": jsonable(log),
            }
        )
    return records
