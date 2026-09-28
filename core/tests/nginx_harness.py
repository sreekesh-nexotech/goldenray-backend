"""Runs the real deploy/nginx configuration against mock upstreams (used by core/tests/test_deploy.py).

The copy differs from production only where a test machine must differ: file paths point into a temp directory,
listen ports are free high ports, container host names become 127.0.0.1 mock servers, IPv6 listeners are dropped,
and the two directives newer than the distro's nginx (``http2 on``, upstream ``resolve``; both nginx ≥ 1.27 in the
nginx:1.28 image) are removed. Each mock upstream answers JSON describing the request it received.
"""

from __future__ import annotations

import datetime as dt
import http.client
import json
import os
import re
import shutil
import socket
import ssl
import subprocess
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

from cryptography import x509
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import rsa
from cryptography.x509.oid import NameOID

NGINX_DIR = Path(__file__).resolve().parents[2] / "deploy" / "nginx"


def _free_ports(count: int) -> list[int]:
    """``count`` distinct free ports (all held until every one is chosen, then released for nginx)."""
    sockets = []
    try:
        for _ in range(count):
            sock = socket.socket()
            sock.bind(("127.0.0.1", 0))
            sockets.append(sock)
        return [sock.getsockname()[1] for sock in sockets]
    finally:
        for sock in sockets:
            sock.close()


def _mock_upstream(name: str) -> ThreadingHTTPServer:
    class Handler(BaseHTTPRequestHandler):
        def _answer(self):
            body = json.dumps(
                {
                    "server": name,
                    "path": self.path,
                    "host": self.headers.get("Host"),
                    "x-forwarded-proto": self.headers.get("X-Forwarded-Proto"),
                    "x-forwarded-for": self.headers.get("X-Forwarded-For"),
                    "x-request-id": self.headers.get("X-Request-ID") or "",
                }
            ).encode()
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        do_GET = do_POST = _answer

        def log_message(self, *args):
            pass

    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    return server


def _self_signed(directory: Path) -> None:
    key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    name = x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, "flarize.com")])
    now = dt.datetime.now(dt.timezone.utc)
    cert = (
        x509.CertificateBuilder()
        .subject_name(name)
        .issuer_name(name)
        .public_key(key.public_key())
        .serial_number(1)
        .not_valid_before(now)
        .not_valid_after(now + dt.timedelta(days=1))
        .sign(key, hashes.SHA256())
    )
    (directory / "fullchain.pem").write_bytes(cert.public_bytes(serialization.Encoding.PEM))
    (directory / "privkey.pem").write_bytes(key.private_bytes(serialization.Encoding.PEM, serialization.PrivateFormat.TraditionalOpenSSL, serialization.NoEncryption()))


class NginxHarness:
    def __init__(self, workdir: Path):
        self.workdir = workdir
        self.binary = shutil.which("nginx") or "/usr/sbin/nginx"
        # Mock upstreams first: they keep their ports, so nginx's ports can never collide with them.
        self.upstreams = {name: _mock_upstream(name) for name in ("api", "frontend", "legacy-backend", "legacy-cms")}
        self.ports = dict(zip(("http", "https", "plain"), _free_ports(3)))
        self.process: subprocess.Popen | None = None

    # -- configuration -------------------------------------------------------------------------------------------
    def _rewrite(self, text: str) -> str:
        work = str(self.workdir)
        up = {name: f"127.0.0.1:{server.server_address[1]}" for name, server in self.upstreams.items()}
        replacements = [
            ("/etc/nginx/flarize", f"{work}/flarize"),
            ("/etc/letsencrypt/live/flarize.com", f"{work}/certs"),
            ("/srv/flarize/media/private", f"{work}/private"),
            ("/var/log/nginx", f"{work}/logs"),
            ("/var/www/certbot", f"{work}/www"),
            ("api-a:8000", up["api"]),
            ("api-b:8000", up["api"]),
            ("frontend:3000", up["frontend"]),
            ("legacy-backend:8000", up["legacy-backend"]),
            ("legacy-cms:8000", up["legacy-cms"]),
        ]
        for old, new in replacements:
            text = text.replace(old, new)
        text = re.sub(r"^\s*http2 on;\s*$", "", text, flags=re.M)
        text = re.sub(r"^\s*listen \[::\]:\d+[^;]*;\s*$", "", text, flags=re.M)
        text = text.replace(" resolve max_fails", " max_fails").replace(" resolve;", ";")
        text = re.sub(r"listen 443 ", f"listen {self.ports['https']} ", text)
        text = re.sub(r"listen 80;", f"listen {self.ports['http']};", text)
        text = re.sub(r"listen 8080;", f"listen {self.ports['plain']};", text)
        return text

    def _write_config(self) -> Path:
        shutil.copytree(NGINX_DIR, self.workdir / "flarize", dirs_exist_ok=True)
        for path in (self.workdir / "flarize").rglob("*.conf"):
            path.write_text(self._rewrite(path.read_text()))
        for name in ("certs", "private", "logs", "www", "tmp"):
            (self.workdir / name).mkdir(exist_ok=True)
        _self_signed(self.workdir / "certs")
        user = "user root;\n" if os.geteuid() == 0 else ""
        temp = self.workdir / "tmp"
        main = self.workdir / "nginx.conf"
        main.write_text(
            f"{user}worker_processes 1;\npid {self.workdir}/nginx.pid;\nerror_log {self.workdir}/logs/error.log;\n"
            "events { worker_connections 64; }\n"
            f"http {{\n  client_body_temp_path {temp}; proxy_temp_path {temp}; fastcgi_temp_path {temp}; uwsgi_temp_path {temp}; scgi_temp_path {temp};\n"
            f"  include {self.workdir}/flarize/flarize.conf;\n}}\n"
        )
        return main

    def set_switch(self, modes: dict[str, str]) -> None:
        """Write a new legacy switch and restart nginx (deterministic, unlike waiting for a graceful reload)."""
        lines = "\n".join(f"  {group} {mode};" for group, mode in sorted(modes.items()))
        (self.workdir / "flarize" / "legacy" / "switch.conf").write_text(f"map $legacy_group $legacy_mode {{\n  default gone;\n{lines}\n}}\n")
        self._stop()
        self._start()

    # -- lifecycle -----------------------------------------------------------------------------------------------
    def _start(self) -> None:
        main = self.workdir / "nginx.conf"
        check = subprocess.run([self.binary, "-t", "-c", str(main), "-p", str(self.workdir)], capture_output=True, text=True)
        if check.returncode != 0:
            raise AssertionError(check.stderr)
        self.process = subprocess.Popen([self.binary, "-c", str(main), "-p", str(self.workdir), "-g", "daemon off;"], stderr=subprocess.PIPE)
        deadline = time.monotonic() + 10
        while time.monotonic() < deadline:
            try:
                socket.create_connection(("127.0.0.1", self.ports["https"]), timeout=0.2).close()
                return
            except OSError:
                time.sleep(0.1)
        raise AssertionError("nginx did not start")

    def _stop(self) -> None:
        if self.process:
            self.process.terminate()
            self.process.wait(timeout=10)
            self.process = None

    def __enter__(self) -> NginxHarness:
        self._write_config()
        self._start()
        return self

    def __exit__(self, *exc) -> None:
        self._stop()
        for server in self.upstreams.values():
            server.shutdown()

    # -- requests ------------------------------------------------------------------------------------------------
    def get(self, path: str, *, port: str = "https") -> tuple[int, dict | None]:
        if port == "https":
            context = ssl.create_default_context()
            context.check_hostname = False
            context.verify_mode = ssl.CERT_NONE
            connection = http.client.HTTPSConnection("127.0.0.1", self.ports["https"], context=context, timeout=10)
        else:
            connection = http.client.HTTPConnection("127.0.0.1", self.ports[port], timeout=10)
        connection.request("GET", path, headers={"Host": "flarize.com"})
        response = connection.getresponse()
        raw = response.read()
        connection.close()
        try:
            return response.status, json.loads(raw)
        except ValueError:
            return response.status, None
