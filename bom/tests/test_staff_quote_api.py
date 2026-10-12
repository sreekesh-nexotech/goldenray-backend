"""POST /api/v1/bom/quote/ and GET /api/v1/bom/quote-options/ — the internal BOM calculator."""

import pytest

pytestmark = pytest.mark.django_db
QUOTE = "/api/v1/bom/quote/"
OPTIONS = "/api/v1/bom/quote-options/"
BODY = {"sys_type": "ongrid", "size": "3", "tier": "base"}


def test_permissions(api_client, outsider, viewer, world):
    assert api_client.post(QUOTE, BODY, format="json").status_code == 401
    assert outsider.post(QUOTE, BODY, format="json").status_code == 403
    assert viewer.post(QUOTE, BODY, format="json").status_code == 200
    assert api_client.get(OPTIONS).status_code == 401
    assert outsider.get(OPTIONS).status_code == 403
    assert viewer.get(OPTIONS).status_code == 200


def test_internal_cost_and_margin_only_with_pricing_internal(auth_client, make_user, world):
    plain = auth_client(make_user(grants={"bom": ["view"]})).post(QUOTE, BODY, format="json")
    assert plain.status_code == 200 and "cost_breakdown" not in plain.json() and "totals" not in plain.json()
    internal = auth_client(make_user(grants={"bom": ["view"], "pricing_internal": ["view"]})).post(QUOTE, BODY, format="json")
    body = internal.json()
    assert internal.status_code == 200 and body["cost_breakdown"]["cost_total"] > 0 and body["totals"]["grand_total"] > 0
    assert body["pricing"] == plain.json()["pricing"] and internal["Cache-Control"] == "no-store"


def test_validation_envelope(viewer, world):
    response = viewer.post(QUOTE, {**BODY, "dist_km": "12.5"}, format="json")
    assert response.status_code == 400 and response.json()["code"] == "validation_error"


def test_quote_options(viewer, world):
    body = viewer.get(OPTIONS).json()
    assert "ongrid" in body["system_types"] and body["structure_types"] == ["flatRoof", "elevated", "sheetRoof"]
    assert set(body["system_types"]["ongrid"]) == {"name", "sizes", "three_phase_sizes", "tiers", "battery_configs"}


# ── Content Studio single sign-in (HS256 token signed by the CMS) ──

STUDIO_KEY = "studio-test-key-0123456789-0123456789"


def _studio_token(email="bom@example.com", **claims):
    import time

    import jwt

    payload = {"token_type": "access", "exp": int(time.time()) + 600, "user_id": 7, "username": "bom", "email": email, **claims}
    return jwt.encode(payload, STUDIO_KEY, algorithm="HS256")


def _studio_client(api_client, token):
    api_client.credentials(HTTP_AUTHORIZATION=f"Bearer {token}")
    return api_client


@pytest.fixture
def studio_user(make_user, settings):
    settings.STUDIO_JWT_SIGNING_KEY = STUDIO_KEY
    return make_user(email="bom@example.com", grants={"bom": ["view"], "pricing_internal": ["view"]})


def test_studio_token_uses_the_platform_role(api_client, studio_user, world):
    response = _studio_client(api_client, _studio_token("BOM@example.com")).post(QUOTE, BODY, format="json")
    assert response.status_code == 200 and "cost_breakdown" in response.json()
    assert _studio_client(api_client, _studio_token("BOM@example.com")).get(OPTIONS).status_code == 200


def test_studio_token_without_bom_grant_is_forbidden(api_client, make_user, settings, world):
    settings.STUDIO_JWT_SIGNING_KEY = STUDIO_KEY
    make_user(email="other@example.com", grants={"catalog": ["view"]})
    assert _studio_client(api_client, _studio_token("other@example.com")).post(QUOTE, BODY, format="json").status_code == 403


@pytest.mark.parametrize(
    "token",
    [
        pytest.param(lambda: _studio_token("nobody@example.com"), id="unknown-email"),
        pytest.param(lambda: _studio_token(email=""), id="no-email"),
        pytest.param(lambda: _studio_token(token_type="refresh"), id="refresh-token"),
        pytest.param(lambda: _studio_token(exp=1), id="expired"),
        pytest.param(lambda: __import__("jwt").encode({"token_type": "access", "email": "bom@example.com"}, "wrong-key-0123456789-0123456789ab", algorithm="HS256"), id="bad-signature"),
    ],
)
def test_bad_studio_tokens_are_401(api_client, studio_user, world, token):
    assert _studio_client(api_client, token()).post(QUOTE, BODY, format="json").status_code == 401


def test_studio_tokens_are_refused_when_not_configured_and_elsewhere(api_client, make_user, settings, world):
    make_user(email="bom@example.com", grants={"bom": ["view"]})
    settings.STUDIO_JWT_SIGNING_KEY = ""
    assert _studio_client(api_client, _studio_token()).post(QUOTE, BODY, format="json").status_code == 401
    settings.STUDIO_JWT_SIGNING_KEY = STUDIO_KEY
    # only the views that opt in accept it: the dry-build view keeps platform tokens only
    assert _studio_client(api_client, _studio_token()).post("/api/v1/bom/build/", {"system_type": "ONGRID", "size": "3", "tier": "BASE"}, format="json").status_code == 401
