"""SessionAwareJWTAuthentication: RS256 access tokens carrying identity only, bound to a live session."""

import base64
import hashlib
import hmac
import json
import time
import uuid
from datetime import timedelta

import jwt
import pytest
from django.conf import settings
from django.db import connection
from django.test.utils import CaptureQueriesContext
from django.utils import timezone
from freezegun import freeze_time
from rest_framework_simplejwt.tokens import AccessToken

from accounts.models import UserSession
from accounts.services import sessions
from audit import context as audit_context

pytestmark = pytest.mark.django_db
URL = "/api/v1/dashboard/"


@pytest.fixture
def user(make_user):
    return make_user(grants={"dashboard": ["view"]})


@pytest.fixture
def issued(user):
    return sessions.start_session(user, ip="127.0.0.1", user_agent="pytest")


def _get(api_client, token):
    return api_client.get(URL, HTTP_AUTHORIZATION=f"Bearer {token}")


def _decode(token):
    return jwt.decode(str(token), settings.SIMPLE_JWT["VERIFYING_KEY"], algorithms=["RS256"], options={"verify_aud": False}, issuer="flarize")


def test_valid_token_authenticates(auth_client, user):
    assert auth_client(user).get(URL).status_code == 200


def test_tokens_carry_identity_and_session_only(issued, user):
    for token in (issued.access, issued.refresh):
        payload = _decode(token)
        assert payload["sub"] == str(user.uid)
        assert payload["sid"] == str(issued.session.uid)
        assert set(payload) == {"token_type", "exp", "iat", "jti", "sub", "sid", "iss"}
        assert jwt.get_unverified_header(token)["alg"] == "RS256"
    assert _decode(issued.access)["token_type"] == "access"
    assert _decode(issued.refresh)["token_type"] == "refresh"
    assert issued.session.refresh_jti.hex == _decode(issued.refresh)["jti"]


def test_token_without_a_session_is_rejected(api_client, user):
    """AccessToken.for_user() mints no `sid`: only the login service can create a usable token."""
    response = _get(api_client, AccessToken.for_user(user))
    assert response.status_code == 401
    assert response.json()["code"] == "not_authenticated"


def test_token_naming_an_unknown_or_foreign_session_is_rejected(api_client, user, make_user):
    other = sessions.start_session(make_user(), ip=None)
    for sid in (str(uuid.uuid4()), "not-a-uuid", str(other.session.uid)):
        token = AccessToken.for_user(user)
        token["sid"] = sid
        assert _get(api_client, token).status_code == 401, sid


def test_revoked_session_invalidates_its_access_token_immediately(api_client, issued, user):
    assert _get(api_client, issued.access).status_code == 200  # liveness now cached
    sessions.revoke_session(issued.session, user=user, reason="test")
    response = _get(api_client, issued.access)
    assert response.status_code == 401
    assert response.json()["code"] == "not_authenticated"
    assert response.json()["error_codes"] == ["not_authenticated", "session_revoked"]
    assert response.json()["message"] == "The session has ended"


def test_revoking_all_sessions_invalidates_every_token(api_client, user):
    first, second = sessions.start_session(user), sessions.start_session(user)
    assert _get(api_client, first.access).status_code == 200 and _get(api_client, second.access).status_code == 200
    assert sessions.revoke_all_sessions(user, user=user, reason="test") == 2
    assert _get(api_client, first.access).status_code == 401
    assert _get(api_client, second.access).status_code == 401


def test_revoking_all_but_one_keeps_it(api_client, user):
    keep, drop = sessions.start_session(user), sessions.start_session(user)
    assert sessions.revoke_all_sessions(user, user=user, reason="test", keep_session_uid=keep.session.uid) == 1
    assert _get(api_client, keep.access).status_code == 200
    assert _get(api_client, drop.access).status_code == 401


def test_session_liveness_is_cached_between_requests(api_client, issued):
    assert _get(api_client, issued.access).status_code == 200
    with CaptureQueriesContext(connection) as queries:
        assert _get(api_client, issued.access).status_code == 200
    assert not any("accounts_user_session" in query["sql"] for query in queries.captured_queries)


def test_expired_session_is_rejected_even_when_liveness_is_cached(api_client, user):
    with freeze_time("2026-01-01 10:00:00") as frozen:
        issued = sessions.start_session(user)
        UserSession.objects.filter(pk=issued.session.pk).update(expires_at=timezone.now() + timedelta(seconds=20))
        assert _get(api_client, issued.access).status_code == 200  # liveness (with its expiry) is now cached
        frozen.tick(timedelta(seconds=25))
        assert _get(api_client, issued.access).status_code == 401


def test_inactive_user_is_rejected(api_client, issued, user):
    user.is_active = False
    user.save()
    response = _get(api_client, issued.access)
    assert response.status_code == 401
    assert response.json()["code"] == "not_authenticated"


def test_user_waiting_for_a_password_reset_is_rejected(api_client, issued, user):
    user.must_reset_password = True
    user.save()
    assert _get(api_client, issued.access).status_code == 401


def test_soft_deleted_user_is_rejected(api_client, issued, user):
    user.soft_delete()
    assert _get(api_client, issued.access).status_code == 401


def test_garbage_subject_is_401_not_500(api_client, issued):
    token = AccessToken(issued.access)
    token["sub"] = "not-a-uuid"
    assert _get(api_client, token).status_code == 401
    token["sub"] = "00000000-0000-0000-0000-000000000000"
    assert _get(api_client, token).status_code == 401


def _claims(user, sid, **overrides):
    now = int(time.time())
    claims = {"sub": str(user.uid), "sid": str(sid), "token_type": "access", "jti": "f" * 32, "iat": now, "exp": now + 600, "iss": "flarize"}
    claims.update(overrides)
    return claims


def _b64(data: bytes) -> str:
    return base64.urlsafe_b64encode(data).rstrip(b"=").decode()


def test_algorithm_confusion_is_rejected(api_client, user, issued):
    """An HS256 token 'signed' with the public key (the classic RS→HS confusion) must not authenticate."""
    header = _b64(json.dumps({"alg": "HS256", "typ": "JWT"}).encode())
    body = _b64(json.dumps(_claims(user, issued.session.uid)).encode())
    signature = _b64(hmac.new(settings.SIMPLE_JWT["VERIFYING_KEY"].encode(), f"{header}.{body}".encode(), hashlib.sha256).digest())
    assert _get(api_client, f"{header}.{body}.{signature}").status_code == 401
    unsigned = jwt.encode(_claims(user, issued.session.uid), None, algorithm="none")
    assert _get(api_client, unsigned).status_code == 401


def test_correctly_signed_hand_made_token_with_a_live_session_is_accepted(api_client, user, issued):
    token = jwt.encode(_claims(user, issued.session.uid), settings.SIMPLE_JWT["SIGNING_KEY"], algorithm="RS256")
    assert _get(api_client, token).status_code == 200


def test_expired_token_is_rejected(api_client, user):
    with freeze_time("2026-01-01 10:00:00"):
        issued = sessions.start_session(user)
    with freeze_time("2026-01-01 10:16:00"):
        assert _get(api_client, issued.access).status_code == 401
    assert settings.SIMPLE_JWT["ACCESS_TOKEN_LIFETIME"] == timedelta(minutes=15)


def test_refresh_token_is_not_an_access_token(api_client, issued):
    assert _get(api_client, issued.refresh).status_code == 401


def test_wrong_issuer_is_rejected(api_client, user, issued):
    token = jwt.encode(_claims(user, issued.session.uid, iss="someone-else"), settings.SIMPLE_JWT["SIGNING_KEY"], algorithm="RS256")
    assert _get(api_client, token).status_code == 401


def test_missing_or_malformed_header(api_client):
    assert api_client.get(URL).status_code == 401
    response = api_client.get(URL, HTTP_AUTHORIZATION="Bearer")
    assert response.status_code == 401
    assert response["WWW-Authenticate"].startswith("Bearer")


def test_authenticated_user_becomes_the_audit_actor(rf, user, issued):
    from accounts.authentication import SessionAwareJWTAuthentication

    request = rf.get(URL, HTTP_AUTHORIZATION=f"Bearer {issued.access}")
    with audit_context.bind(request_id=str(uuid.uuid4())):
        authenticated, _ = SessionAwareJWTAuthentication().authenticate(request)
        assert audit_context.current().actor == user == authenticated
        assert audit_context.current().actor_kind == "USER"
    assert audit_context.current().actor is None
