"""Bunny client (public backend in staging/prod): checksum header, errors, config from env or company_integration."""

import hashlib
import re

import pytest
import responses
from django.core.exceptions import ImproperlyConfigured

from core import integrations
from media.models import MediaAsset
from media.services import bunny
from media.services.bunny import BunnyClient, BunnyConfig, BunnyError, BunnyNotFound
from media.tests import files
from media.tests.factories import upload

CONFIG = BunnyConfig(storage_zone="flarize-media", storage_endpoint="sg.storage.bunnycdn.com", access_key="k" * 36, cdn_base_url="https://cdn.flarize.test")
BASE = "https://sg.storage.bunnycdn.com/flarize-media"


@pytest.fixture
def bunny_env(settings):
    settings.MEDIA_PUBLIC_BACKEND = "bunny"
    settings.BUNNY_STORAGE_ZONE = CONFIG.storage_zone
    settings.BUNNY_STORAGE_ENDPOINT = CONFIG.storage_endpoint
    settings.BUNNY_STORAGE_ACCESS_KEY = CONFIG.access_key
    settings.BUNNY_CDN_BASE_URL = CONFIG.cdn_base_url
    return settings


@responses.activate
def test_put_sends_access_key_and_checksum():
    responses.put(f"{BASE}/a/b%20c.jpg", status=201)
    BunnyClient(CONFIG).put("a/b c.jpg", b"data")
    request = responses.calls[0].request
    assert request.headers["AccessKey"] == CONFIG.access_key
    assert request.headers["Checksum"] == hashlib.sha256(b"data").hexdigest().upper()
    assert request.body == b"data"


@responses.activate
def test_errors():
    responses.put(f"{BASE}/x", status=401)
    responses.get(f"{BASE}/missing", status=404)
    responses.get(f"{BASE}/broken", status=500)
    responses.delete(f"{BASE}/gone", status=404)
    responses.delete(f"{BASE}/nope", status=403)
    client = BunnyClient(CONFIG)
    with pytest.raises(BunnyError):
        client.put("x", b"1")
    with pytest.raises(BunnyNotFound):
        client.get("missing")
    with pytest.raises(BunnyError):
        client.get("broken")
    client.delete("gone")  # already gone is fine
    with pytest.raises(BunnyError):
        client.delete("nope")


@responses.activate
def test_network_failure_is_a_bunny_error():
    with pytest.raises(BunnyError):
        BunnyClient(CONFIG).get("unreachable")  # no mock registered → ConnectionError


def test_repr_hides_the_key():
    assert CONFIG.access_key not in repr(CONFIG)


@pytest.mark.django_db
def test_config_from_env(bunny_env):
    assert bunny.load_config() == CONFIG


def test_stored_integration_wins_over_env(bunny_env, monkeypatch):
    stored = {"storage_zone": "zone-b", "access_key": "s" * 30, "cdn_base_url": "https://cdn-b.test"}
    monkeypatch.setattr(integrations, "_resolver", lambda key: stored if key == "BUNNY" else None)
    config = bunny.load_config()
    assert (config.storage_zone, config.storage_endpoint, config.cdn_base_url) == ("zone-b", "storage.bunnycdn.com", "https://cdn-b.test")


@pytest.mark.django_db
@pytest.mark.parametrize("missing", ["BUNNY_STORAGE_ZONE", "BUNNY_STORAGE_ACCESS_KEY", "BUNNY_CDN_BASE_URL"])
def test_incomplete_config_is_refused(bunny_env, missing):
    setattr(bunny_env, missing, "")
    with pytest.raises(ImproperlyConfigured):
        bunny.load_config()


@pytest.mark.django_db
def test_cdn_must_be_https(bunny_env):
    bunny_env.BUNNY_CDN_BASE_URL = "http://cdn.flarize.test"
    with pytest.raises(ImproperlyConfigured):
        bunny.load_config()


@pytest.mark.django_db
@responses.activate
def test_public_upload_goes_to_bunny_and_private_never_does(bunny_env, auth_client, make_user, media_roots):
    responses.put(re.compile(rf"^{BASE}/library/\d{{4}}/\d{{2}}/[0-9a-f]{{32}}\.jpg$"), status=201)
    client = auth_client(make_user(grants={"media": "*"}))
    response = client.post("/api/v1/media/upload/", {"file": upload("a.jpg", files.jpeg()), "visibility": "PUBLIC", "kind": "IMAGE"}, format="multipart")
    assert response.status_code == 201, response.json()
    asset = MediaAsset.objects.get(uid=response.json()["uid"])
    assert asset.cdn_url == f"https://cdn.flarize.test/{asset.file}" == response.json()["url"]
    assert len(responses.calls) == 1
    response = client.post("/api/v1/media/upload/", {"file": upload("a.pdf", files.pdf()), "visibility": "PRIVATE", "kind": "DOCUMENT"}, format="multipart")
    assert response.status_code == 201 and len(responses.calls) == 1  # private upload never touched Bunny
    assert (media_roots / "private" / MediaAsset.objects.get(uid=response.json()["uid"]).file).exists()


@pytest.mark.django_db
def test_unconfigured_bunny_is_503(bunny_env, auth_client, make_user):
    bunny_env.BUNNY_STORAGE_ACCESS_KEY = ""
    client = auth_client(make_user(grants={"media": "*"}))
    response = client.post("/api/v1/media/upload/", {"file": upload("a.jpg", files.jpeg()), "visibility": "PUBLIC", "kind": "IMAGE"}, format="multipart")
    assert response.status_code == 503 and response.json()["code"] == "storage_unavailable"
