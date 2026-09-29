# Flarize office agent (`essl_agent`)

One always-on PC per office reads the eSSL/ZK terminals on the office LAN (read-only, ZK/TCP via `pyzk`) and delivers
users and punches to the Flarize platform over outbound HTTPS (`/api/agent/v1/`). No inbound port, static address or
VPN is needed. Not a Django app: standard library + `pyzk`.

```
pip install -r requirements.txt            # pyzk==0.9
python -m essl_agent --config agent.ini run            # continuous operation
python -m essl_agent --config agent.ini once           # one read + deliver cycle
python -m essl_agent --config agent.ini discover       # find this office's terminals on this LAN (by serial)
python -m essl_agent --config agent.ini test-device --ip 192.168.1.209   # read-only terminal check
python -m essl_agent --config agent.ini status         # local queue
```

`agent.ini` is downloaded from Studio (Devices → Agents → config download, one time); `agent.ini.example` documents
every key. The token (`fl_<prefix>_<secret>`) is a secret: keep the file on the office machine only.

Guarantees (see `essl_agent/runner.py` and `docs/decisions/devices.md`): only hardware answering with the pinned
serial is read (an IDENTITY_MISMATCH holds the queue, is reported, and survives restarts); nothing read is lost to an
outage (SQLite queue, retried with backoff, content-derived `Idempotency-Key`); no request is made when there is
nothing to say; the terminal is never written to.

Tests (pyzk and the platform replaced by doubles): `pytest essl-agent/tests` from the repository root, or
`pytest tests` from this directory.
