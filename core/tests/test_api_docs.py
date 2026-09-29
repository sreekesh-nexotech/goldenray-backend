"""``/api/docs/`` and ``/api/schema/<version>/``: reachable from the allow-listed networks without a JWT (a browser
navigation cannot send one), denied elsewhere, and every error in the JSON envelope (PLAN §3.1, §5.4)."""

from __future__ import annotations

import json
import os
import subprocess
import sys

import pytest
from cryptography.fernet import Fernet
from django.conf import settings

from flarize.keys import generate_rsa_private_pem, public_pem_from_private

ENVELOPE_KEYS = {"code", "message", "errors", "error_codes"}
DOCS, SCHEMA = "/api/docs/", "/api/schema/v1/"


def _is_envelope(response, status, code):
    assert response.status_code == status, response.content[:200]
    assert response["Content-Type"] == "application/json", response["Content-Type"]
    body = response.json()
    assert set(body) == ENVELOPE_KEYS and body["code"] == code
    return body


class TestErrorsAreJson:
    def test_wrong_method_on_the_swagger_shell(self, api_client):
        _is_envelope(api_client.delete(DOCS), 405, "method_not_allowed")

    def test_wrong_method_on_the_schema_is_not_yaml(self, api_client):
        _is_envelope(api_client.post(SCHEMA, {}, format="json"), 405, "method_not_allowed")

    def test_unknown_format_on_the_schema(self, api_client):
        _is_envelope(api_client.get(SCHEMA + "?format=xml"), 404, "not_found")


@pytest.mark.django_db
class TestNetworkAllowList:
    @pytest.fixture(autouse=True)
    def private_docs(self, settings):
        settings.API_DOCS_PUBLIC = False
        settings.API_DOCS_ALLOWED_NETWORKS = ["198.51.100.0/24"]

    @pytest.mark.parametrize("path", [DOCS, SCHEMA])
    def test_an_allow_listed_network_needs_no_token(self, api_client, path):
        assert api_client.get(path, REMOTE_ADDR="198.51.100.7").status_code == 200

    @pytest.mark.parametrize("path", [DOCS, SCHEMA])
    def test_any_other_network_is_denied_in_the_envelope(self, api_client, path):
        body = _is_envelope(api_client.get(path, REMOTE_ADDR="192.0.2.1"), 403, "permission_denied")
        assert body["message"] == "The API documentation is only available from the allow-listed office networks."

    def test_an_empty_allow_list_denies_everyone(self, api_client, settings):
        settings.API_DOCS_ALLOWED_NETWORKS = []
        assert api_client.get(SCHEMA, REMOTE_ADDR="198.51.100.7").status_code == 403

    def test_the_client_ip_comes_from_the_trusted_proxy_chain(self, api_client):
        # 10.0.0.0/8 is a trusted proxy in the test settings: the forwarded client address decides.
        assert api_client.get(SCHEMA, REMOTE_ADDR="10.0.0.5", HTTP_X_FORWARDED_FOR="198.51.100.9").status_code == 200
        assert api_client.get(SCHEMA, REMOTE_ADDR="10.0.0.5", HTTP_X_FORWARDED_FOR="192.0.2.1").status_code == 403
        # an untrusted peer cannot claim an allow-listed address
        assert api_client.get(SCHEMA, REMOTE_ADDR="192.0.2.1", HTTP_X_FORWARDED_FOR="198.51.100.9").status_code == 403

    def test_a_jwt_does_not_bypass_the_allow_list(self, auth_client, make_user):
        assert auth_client(make_user(grants={"company": ["view"]})).get(SCHEMA, REMOTE_ADDR="192.0.2.1").status_code == 403

    def test_public_docs_ignore_the_allow_list(self, api_client, settings):
        settings.API_DOCS_PUBLIC = True
        assert api_client.get(DOCS, REMOTE_ADDR="192.0.2.1").status_code == 200


PROD_PROBE = """
import django, json
django.setup()
from django.test import Client
client = Client()
out = {}
for name, path, ip in (("docs_allowed", "/api/docs/", "198.51.100.7"), ("docs_denied", "/api/docs/", "192.0.2.1"), ("schema_denied", "/api/schema/v1/", "192.0.2.1")):
    response = client.get(path, secure=True, HTTP_HOST="flarize.com", REMOTE_ADDR=ip)
    out[name] = [response.status_code, response["Content-Type"]]
print(json.dumps(out))
"""


def test_prod_docs_are_usable_from_the_allow_listed_network(tmp_path):
    """The Swagger shell is a browser navigation: under prod settings it must load without a JWT (F-FIX)."""
    private = tmp_path / "jwt_private.pem"
    private.write_bytes(generate_rsa_private_pem())
    public = tmp_path / "jwt_public.pem"
    public.write_text(public_pem_from_private(private.read_bytes()))
    env = {key: value for key, value in os.environ.items() if not key.startswith(("DJANGO_", "API_DOCS", "TRUSTED", "ALLOWED"))}
    env.update(
        {
            "DJANGO_SETTINGS_MODULE": "flarize.settings.prod",
            "SECRET_KEY": "k" * 64,
            "ALLOWED_HOSTS": "flarize.com",
            "JWT_PRIVATE_KEY_PATH": str(private),
            "JWT_PUBLIC_KEY_PATH": str(public),
            "FERNET_KEYS": Fernet.generate_key().decode(),
            "TRUSTED_PROXIES": "172.28.0.0/16",
            "PASSWORD_RESET_URL": "https://flarize.com/studio/reset-password",
            "FRONTEND_BASE_URL": "https://flarize.com",
            "EMAIL_BACKEND": "django.core.mail.backends.smtp.EmailBackend",
            "MEDIA_PUBLIC_BACKEND": "bunny",
            "DOCUMENTS_RENDERER": "playwright",
            "PRIVATE_MEDIA_ROOT": str(tmp_path / "private"),
            "PUBLIC_MEDIA_ROOT": str(tmp_path / "public"),
            "API_DOCS_ALLOWED_NETWORKS": "198.51.100.0/24",
            "DB_APP_ROLE": "flarize_app",
        }
    )
    result = subprocess.run([sys.executable, "-c", PROD_PROBE], cwd=settings.BASE_DIR, env=env, capture_output=True, text=True, timeout=120)
    assert result.returncode == 0, result.stderr[-2000:]
    out = json.loads(result.stdout.strip().splitlines()[-1])
    assert out["docs_allowed"][0] == 200 and out["docs_allowed"][1].startswith("text/html")
    assert out["docs_denied"] == [403, "application/json"]
    assert out["schema_denied"] == [403, "application/json"]
