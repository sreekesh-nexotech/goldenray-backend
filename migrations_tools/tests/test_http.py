"""The HTTP clients of the verification, against a local stand-in server (never the shared legacy servers)."""

import json
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import pytest

from migrations_tools.services.http import RemoteClient, head_status, query_string


class Handler(BaseHTTPRequestHandler):
    def log_message(self, *args):
        pass

    def _send(self, status, body=b""):
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self):  # noqa: N802 - http.server API
        if self.path.startswith("/missing"):
            self._send(404, b'{"detail": "no"}')
        elif self.path.startswith("/text"):
            self._send(200, b"not json")
        else:
            self._send(200, json.dumps({"path": self.path}).encode())

    def do_HEAD(self):  # noqa: N802
        self._send(200 if self.path == "/ok.png" else 404)

    def do_POST(self):  # noqa: N802
        body = self.rfile.read(int(self.headers["Content-Length"]))
        self._send(201, body)


@pytest.fixture
def server():
    httpd = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    thread = threading.Thread(target=httpd.serve_forever, daemon=True)
    thread.start()
    yield f"http://127.0.0.1:{httpd.server_address[1]}"
    httpd.shutdown()


def test_remote_client(server):
    client = RemoteClient(server + "/")
    assert client.get("/api/articles", [("filters[slug][$eq]", "a b"), ("populate", "*")]) == (200, {"path": "/api/articles?filters%5Bslug%5D%5B%24eq%5D=a+b&populate=%2A"})
    assert client.get("/missing") == (404, {"detail": "no"})
    assert client.get("/text") == (200, None)
    assert client.post("/echo", b'{"a": 1}') == (201, {"a": 1})


def test_head_status(server):
    assert head_status(f"{server}/ok.png") == 200
    assert head_status(f"{server}/gone.png") == 404
    assert head_status("http://127.0.0.1:1/unreachable") == 0
    assert head_status("not a url") == 0


def test_query_string_keeps_order():
    assert query_string({"b": 1, "a": 2}) == "b=1&a=2" and query_string(None) == ""
