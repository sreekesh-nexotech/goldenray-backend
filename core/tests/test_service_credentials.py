import re
from datetime import timedelta
from unittest import mock

import pytest
from django.utils import timezone
from freezegun import freeze_time

from accounts.services.authz import get_grants
from accounts.tests.factories import RoleFactory
from core import service_credentials
from core.errors import Conflict, DomainError
from core.models import ServiceCredential
from core.service_credentials import ServicePrincipal, hash_token, issue, parse_token, revoke, rotate, verify

pytestmark = pytest.mark.django_db
TOKEN_RE = re.compile(r"^fl_[0-9a-f]{12}_[A-Za-z0-9_\-]{40,}$")


def test_issue_returns_plaintext_once_and_stores_only_the_hash(make_user):
    user = make_user()
    credential, token = issue("AGENT", "Office 1 PC", user=user)
    assert TOKEN_RE.match(token)
    stored = ServiceCredential.objects.get(pk=credential.pk)
    assert stored.token_hash == hash_token(token) and len(stored.token_hash) == 64
    assert stored.token_prefix == parse_token(token)
    assert token not in {str(value) for value in stored.__dict__.values()}
    assert stored.created_by == user and stored.issued_at is not None


def test_issue_binds_an_object():
    role = RoleFactory()
    credential, _ = issue("AGENT", "bound", role)
    assert ServiceCredential.objects.get(pk=credential.pk).bound_object == role


def test_issue_validates_input():
    with pytest.raises(DomainError):
        issue("ROBOT", "x")
    with pytest.raises(DomainError):
        issue("AGENT", "")


def test_verify():
    credential, token = issue("AGENT", "pc")
    assert verify(token) == credential
    assert verify(token[:-1] + ("A" if token[-1] != "A" else "B")) is None
    assert verify("fl_000000000000_whatever") is None
    for malformed in ["", "fl_", "Bearer x", "fl_zzzzzzzzzzzz_secret", "xx_" + token[3:], None]:
        assert verify(malformed) is None


def test_verify_always_compares_in_constant_time():
    _, token = issue("AGENT", "pc")
    with mock.patch.object(service_credentials.hmac, "compare_digest", wraps=service_credentials.hmac.compare_digest) as compare:
        verify(token)
        verify("fl_000000000000_unknownprefix")
        verify("malformed")
    assert compare.call_count == 3


def test_rotate_invalidates_the_old_token(make_user):
    credential, old = issue("AGENT", "pc")
    rotated, new = rotate(credential, user=make_user(), expected_version=1)
    assert new != old and TOKEN_RE.match(new)
    assert verify(old) is None
    assert verify(new) == rotated
    assert rotated.version == 2


def test_rotate_checks_version_and_refuses_revoked():
    credential, _ = issue("AGENT", "pc")
    with pytest.raises(Conflict) as excinfo:
        rotate(credential, expected_version=7)
    assert excinfo.value.code == "stale_version"
    revoke(credential)
    with pytest.raises(Conflict) as excinfo:
        rotate(credential)
    assert excinfo.value.code == "credential_revoked"


def test_revoke_is_idempotent_and_blocks_auth():
    credential, token = issue("AGENT", "pc")
    revoked = revoke(credential)
    assert revoked.revoked_at is not None
    assert revoke(credential).version == revoked.version
    assert verify(token) is None


def test_soft_deleted_credentials_do_not_authenticate():
    credential, token = issue("AGENT", "pc")
    credential.soft_delete()
    assert verify(token) is None


def test_last_used_is_updated_at_most_once_per_minute():
    credential, _ = issue("AGENT", "pc")
    with freeze_time("2026-09-28 10:00:00"):
        service_credentials.touch_last_used(credential)
        first = ServiceCredential.objects.get(pk=credential.pk).last_used_at
    with freeze_time("2026-09-28 10:00:30"):
        service_credentials.touch_last_used(credential)
        assert ServiceCredential.objects.get(pk=credential.pk).last_used_at == first
    with freeze_time("2026-09-28 10:01:01"):
        service_credentials.touch_last_used(credential)
        assert ServiceCredential.objects.get(pk=credential.pk).last_used_at == first + timedelta(seconds=61)
    assert ServiceCredential.objects.get(pk=credential.pk).version == 1


def test_service_principal_has_no_staff_grants():
    credential, _ = issue("AGENT", "pc")
    principal = ServicePrincipal(credential)
    assert principal.is_authenticated and not principal.is_anonymous
    assert principal.pk == f"svc:{credential.uid}"
    assert get_grants(principal).permissions == {}


@pytest.mark.urls("core.tests.urls_testing")
class TestAuthentication:
    url = "/api/agent/v1/_t/ping/"

    def test_valid_token(self, api_client):
        _, token = issue("AGENT", "Office PC")
        response = api_client.get(self.url, HTTP_AUTHORIZATION=f"Bearer {token}")
        assert response.status_code == 200
        assert response.json() == {"principal": "AGENT:Office PC", "kind": "AGENT"}
        assert ServiceCredential.objects.get().last_used_at is not None

    @pytest.mark.parametrize("header", [None, "Bearer", "Bearer fl_000000000000_nope", "Bearer a b", "Token abc"])
    def test_invalid_or_missing_token_is_401(self, api_client, header):
        kwargs = {"HTTP_AUTHORIZATION": header} if header else {}
        response = api_client.get(self.url, **kwargs)
        assert response.status_code == 401
        assert response.json()["code"] == "not_authenticated"
        assert response["WWW-Authenticate"] == 'Bearer realm="api"'

    def test_revoked_token_is_401(self, api_client):
        credential, token = issue("AGENT", "pc")
        revoke(credential)
        assert api_client.get(self.url, HTTP_AUTHORIZATION=f"Bearer {token}").status_code == 401

    def test_staff_jwt_is_not_a_service_token(self, api_client, make_user, auth_client):
        client = auth_client(make_user(grants={"dashboard": ["view"]}))
        assert client.get(self.url).status_code == 401

    def test_service_token_cannot_reach_staff_endpoints(self, api_client):
        _, token = issue("AGENT", "pc")
        response = api_client.get("/api/v1/dashboard/", HTTP_AUTHORIZATION=f"Bearer {token}")
        assert response.status_code == 401


def test_issue_retries_on_prefix_collision():
    existing, _ = issue("AGENT", "first")
    tokens = iter([(existing.token_prefix, f"fl_{existing.token_prefix}_dup"), ("abcdefabcdef", "fl_abcdefabcdef_fresh")])
    with mock.patch.object(service_credentials, "_new_token", side_effect=lambda: next(tokens)):
        credential, token = issue("AGENT", "second")
    assert token == "fl_abcdefabcdef_fresh" and credential.token_prefix == "abcdefabcdef"


def test_issued_at_is_now():
    before = timezone.now()
    credential, _ = issue("AGENT", "pc")
    assert credential.issued_at >= before
