"""Flarize office agent: reads the eSSL/ZK terminals on an office LAN (read-only) and delivers to the platform.

    terminal (LAN, ZK/TCP) -> agent -> durable SQLite queue -> HTTPS /api/agent/v1/ -> Flarize platform

Standalone package (not a Django app): standard library plus ``pyzk`` for the terminal protocol.
"""

VERSION = "2.0.0"
