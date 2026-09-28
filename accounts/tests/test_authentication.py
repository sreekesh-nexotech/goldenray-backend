"""SessionAwareJWTAuthentication: RS256 access tokens carrying identity only."""

import base64
import hashlib
import hmac
import json
import time
from datetime import timedelta

import jwt
import pytest
from django.conf import settings
from freezegun import freeze_time
from rest_framework_simplejwt.tokens import AccessToken, RefreshToken

pytestmark = pytest.mark.django_db
URL = "/api/v1/dashboard/"


@pytest.fixture
def user(make_user):
    return make_user(grants={"dashboard": ["view"]})


def test_valid_token_authenticates(auth_client, user):
    assert auth_client(user).get(URL).status_code == 200


def test_token_carries_identity_only(user):
    payload = jwt.decode(str(AccessToken.for_user(user)), settings.SIMPLE_JWT["VERIFYING_KEY"], algorithms=["RS256"], options={"verify_aud": False})
    assert payload["sub"] == str(user.uid)
    assert set(payload) <= {"token_type", "exp", "iat", "jti", "sub", "iss"}
    assert jwt.get_unverified_header(str(AccessToken.for_user(user)))["alg"] == "RS256"


def _get(api_client, token):
    return api_client.get(URL, HTTP_AUTHORIZATION=f"Bearer {token}")


def test_inactive_user_is_rejected(api_client, user):
    token = AccessToken.for_user(user)
    user.is_active = False
    user.save()
    response = _get(api_client, token)
    assert response.status_code == 401
    assert response.json()["code"] == "not_authenticated"


def test_soft_deleted_user_is_rejected(api_client, user):
    token = AccessToken.for_user(user)
    user.soft_delete()
    assert _get(api_client, token).status_code == 401


def test_garbage_subject_is_401_not_500(api_client, user):
    token = AccessToken.for_user(user)
    token["sub"] = "not-a-uuid"
    assert _get(api_client, token).status_code == 401
    token["sub"] = "00000000-0000-0000-0000-000000000000"
    assert _get(api_client, token).status_code == 401


def _claims(user, **overrides):
    now = int(time.time())
    claims = {"sub": str(user.uid), "token_type": "access", "jti": "f" * 32, "iat": now, "exp": now + 600, "iss": "flarize"}
    claims.update(overrides)
    return claims


def _b64(data: bytes) -> str:
    return base64.urlsafe_b64encode(data).rstrip(b"=").decode()


def test_algorithm_confusion_is_rejected(api_client, user):
    """An HS256 token 'signed' with the public key (the classic RS→HS confusion) must not authenticate."""
    header = _b64(json.dumps({"alg": "HS256", "typ": "JWT"}).encode())
    body = _b64(json.dumps(_claims(user)).encode())
    signature = _b64(hmac.new(settings.SIMPLE_JWT["VERIFYING_KEY"].encode(), f"{header}.{body}".encode(), hashlib.sha256).digest())
    assert _get(api_client, f"{header}.{body}.{signature}").status_code == 401
    unsigned = jwt.encode(_claims(user), None, algorithm="none")
    assert _get(api_client, unsigned).status_code == 401


def test_correctly_signed_hand_made_token_is_accepted(api_client, user):
    token = jwt.encode(_claims(user), settings.SIMPLE_JWT["SIGNING_KEY"], algorithm="RS256")
    assert _get(api_client, token).status_code == 200


def test_expired_token_is_rejected(api_client, user):
    with freeze_time("2026-01-01 10:00:00"):
        token = AccessToken.for_user(user)
    with freeze_time("2026-01-01 10:16:00"):
        assert _get(api_client, token).status_code == 401
    assert settings.SIMPLE_JWT["ACCESS_TOKEN_LIFETIME"] == timedelta(minutes=15)


def test_refresh_token_is_not_an_access_token(api_client, user):
    assert _get(api_client, RefreshToken.for_user(user)).status_code == 401


def test_wrong_issuer_is_rejected(api_client, user):
    token = jwt.encode(_claims(user, iss="someone-else"), settings.SIMPLE_JWT["SIGNING_KEY"], algorithm="RS256")
    assert _get(api_client, token).status_code == 401


def test_missing_or_malformed_header(api_client):
    assert api_client.get(URL).status_code == 401
    response = api_client.get(URL, HTTP_AUTHORIZATION="Bearer")
    assert response.status_code == 401
    assert response["WWW-Authenticate"].startswith("Bearer")
