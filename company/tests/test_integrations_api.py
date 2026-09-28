"""settings/integrations/ — schema-validated provider settings, write-only secrets, Fernet at rest, the resolver."""

from unittest import mock

import pytest
from django.core import mail
from django.core.mail.backends.smtp import EmailBackend as SmtpBackend
from django.db import connection

from audit.models import AuditLog
from company.models import Integration
from company.tests.factories import IntegrationFactory
from core import integrations, notifications

pytestmark = pytest.mark.django_db
URL = "/api/v1/settings/integrations/"
SMTP = {"host": "smtp.zoho.in", "port": 465, "username": "mailer@flarize.com", "password": "Very-Secret-Pass-1", "use_tls": False, "use_ssl": True, "from_email": "Flarize <no-reply@flarize.com>"}


@pytest.fixture
def admin(make_user):
    return make_user(grants={"settings": ["view", "edit"]})


@pytest.fixture
def client(auth_client, admin):
    return auth_client(admin)


def put(client, key="SMTP", config=None, enabled=True, **extra):
    return client.put(URL, {"key": key, "is_enabled": enabled, "config": SMTP if config is None else config, **extra}, format="json")


class TestPermissions:
    def test_anonymous_is_401(self, api_client):
        assert api_client.get(URL).status_code == 401 and api_client.put(URL, {}, format="json").status_code == 401

    def test_view_cannot_put(self, auth_client, make_user):
        client = auth_client(make_user(grants={"settings": ["view"]}))
        assert client.get(URL).status_code == 200 and put(client).status_code == 403
        assert auth_client(make_user(grants={"company": "*"})).get(URL).status_code == 403


class TestReadAndWrite:
    def test_list_describes_every_provider(self, client):
        body = client.get(URL).json()
        assert [row["key"] for row in body] == ["TWILIO", "BUNNY", "SMTP"]
        smtp = body[2]
        assert smtp["uid"] is None and smtp["is_enabled"] is False and smtp["secrets"] == {"password": False}
        assert smtp["config"]["port"] == 587 and "password" not in smtp["config"]
        assert {"name": "password", "type": "str", "required": False, "secret": True, "default": None} in smtp["fields"]

    def test_put_stores_encrypted_and_never_returns_secrets(self, client, admin):
        response = put(client)
        assert response.status_code == 200, response.json()
        body = response.json()
        assert body["is_enabled"] is True and body["secrets"] == {"password": True} and body["config"]["host"] == "smtp.zoho.in"
        assert "Very-Secret-Pass-1" not in response.content.decode() and "Very-Secret-Pass-1" not in client.get(URL).content.decode()
        with connection.cursor() as cursor:
            cursor.execute("SELECT config FROM company_integration WHERE key = 'SMTP'")
            raw = cursor.fetchone()[0]
        assert raw.startswith("gAAAA") and "Very-Secret-Pass-1" not in raw and "smtp.zoho.in" not in raw
        entry = AuditLog.objects.get(action="company.integration_updated")
        assert "Very-Secret-Pass-1" not in str(entry.after) and entry.after["write_only_fields_changed"] == ["password"] and entry.actor == admin

    def test_secret_is_kept_when_omitted_and_cleared_with_null(self, client):
        put(client)
        without_password = {name: value for name, value in SMTP.items() if name != "password"}
        body = put(client, config={**without_password, "host": "smtp.gmail.com"}, expected_version=1).json()
        assert body["secrets"] == {"password": True} and body["config"]["host"] == "smtp.gmail.com" and body["version"] == 2
        assert Integration.objects.get(key="SMTP").config["password"] == "Very-Secret-Pass-1"
        body = put(client, config={**without_password, "password": None}).json()
        assert body["secrets"] == {"password": False} and "password" not in Integration.objects.get(key="SMTP").config

    def test_unchanged_put_is_not_a_write(self, client):
        put(client)
        assert put(client).json()["version"] == 1
        assert AuditLog.objects.filter(action="company.integration_updated").count() == 1

    @pytest.mark.parametrize(
        "key,config,enabled,field",
        [
            ("SMTP", {**SMTP, "surprise": 1}, True, "config"),
            ("SMTP", {**SMTP, "port": 70000}, True, "config.port"),
            ("SMTP", {**SMTP, "use_tls": True, "use_ssl": True}, True, "config.use_ssl"),
            ("SMTP", {"port": 25}, True, "config.host"),
            ("SMTP", {**SMTP, "from_email": "not an address"}, True, "config.from_email"),
            ("BUNNY", {"storage_zone": "z", "storage_endpoint": "evil.example.com", "cdn_base_url": "https://cdn.x.test", "access_key": "a" * 30}, True, "config.storage_endpoint"),
            ("BUNNY", {"storage_zone": "z", "cdn_base_url": "http://cdn.x.test", "access_key": "a" * 30}, True, "config.cdn_base_url"),
            ("BUNNY", {"storage_zone": "z", "cdn_base_url": "https://cdn.x.test"}, True, "config.access_key"),
            ("TWILIO", {"account_sid": "AC123", "verify_service_sid": "VA" + "a" * 32, "auth_token": "t" * 32}, True, "config.account_sid"),
        ],
    )
    def test_validation(self, client, key, config, enabled, field):
        response = put(client, key=key, config=config, enabled=enabled)
        assert response.status_code == 400 and field in response.json()["errors"], response.json()

    def test_disabled_integration_may_be_incomplete(self, client):
        response = put(client, key="TWILIO", config={"account_sid": "AC" + "0" * 32}, enabled=False)
        assert response.status_code == 200 and response.json()["is_enabled"] is False

    def test_stale_version(self, client):
        put(client)
        put(client, config={**SMTP, "port": 587})
        assert put(client, config={**SMTP, "port": 2525}, expected_version=1).status_code == 409


class TestResolver:
    def test_company_installs_the_resolver(self):
        from company.services.integrations import stored_config

        assert integrations._resolver is stored_config

    def test_only_enabled_rows_are_resolved(self):
        IntegrationFactory(is_enabled=False)
        assert integrations.get_config("SMTP") is None
        Integration.objects.update(is_enabled=True)
        assert integrations.get_config("SMTP")["password"] == "smtp-secret-value"
        assert integrations.get_config("TWILIO") is None
        with pytest.raises(ValueError):
            integrations.get_config("SLACK")

    def test_smtp_integration_drives_delivery_only_on_a_real_smtp_backend(self, settings):
        IntegrationFactory()
        settings.EMAIL_BACKEND = "django.core.mail.backends.locmem.EmailBackend"
        notifications.send_email(to="a@example.com", subject="Hi", text="x")
        assert len(mail.outbox) == 1 and mail.outbox[0].from_email == settings.DEFAULT_FROM_EMAIL
        settings.EMAIL_BACKEND = notifications.SMTP_BACKEND
        with mock.patch.object(SmtpBackend, "send_messages", autospec=True, return_value=1) as send:
            assert notifications.send_email(to="a@example.com", subject="Hi", text="x") == 1
        backend, messages = send.call_args.args
        assert (backend.host, backend.port, backend.username, backend.password, backend.use_tls, backend.use_ssl) == ("smtp.example.com", 587, "mailer", "smtp-secret-value", True, False)
        assert messages[0].from_email == "Flarize <no-reply@flarize.com>"

    def test_env_settings_without_an_integration(self, settings):
        settings.EMAIL_BACKEND = notifications.SMTP_BACKEND
        assert notifications.smtp_override() == (None, settings.DEFAULT_FROM_EMAIL)
