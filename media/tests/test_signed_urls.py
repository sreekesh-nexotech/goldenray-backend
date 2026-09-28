"""Private files: short-lived signed URLs, tamper/expiry refusal, X-Accel delivery, never a public URL."""

from datetime import timedelta

import pytest
from django.utils import timezone
from freezegun import freeze_time

from documents.services.downloads import _signer as documents_signer
from media.tests import files
from media.tests.factories import stored_asset

pytestmark = pytest.mark.django_db


@pytest.fixture
def user(make_user):
    return make_user(grants={"media": ["view"]})


@pytest.fixture
def client(auth_client, user):
    return auth_client(user)


@pytest.fixture
def private_pdf(user):
    return stored_asset(user, data=files.pdf(), name="Contract (signed).pdf", visibility="PRIVATE", kind="DOCUMENT")


def signed(client, asset):
    response = client.get(f"/api/v1/media/{asset.uid}/signed-url/")
    assert response.status_code == 200
    return response.json()


def test_signed_url_downloads_the_private_file(client, api_client, private_pdf):
    body = signed(client, private_pdf)
    assert body["url"].startswith("http://testserver/api/v1/media/download/") and body["thumbnail_url"] is None
    expires = timezone.datetime.fromisoformat(body["expires_at"])
    assert timedelta(minutes=9) < expires - timezone.now() <= timedelta(minutes=10)
    response = api_client.get(body["url"])  # no JWT: the signature is the capability
    assert response.status_code == 200
    assert b"".join(response.streaming_content) == files.pdf()
    assert response["Content-Type"] == "application/pdf"
    assert response["Cache-Control"] == "private, no-store" and response["X-Content-Type-Options"] == "nosniff"
    assert response["Content-Security-Policy"] == "default-src 'none'; sandbox"
    assert response["Content-Disposition"] == "inline; filename=\"Contract (signed).pdf\"; filename*=UTF-8''Contract%20%28signed%29.pdf"
    assert api_client.get(body["url"]).status_code == 200  # reusable until it expires


def test_x_accel_redirect_in_production_mode(client, api_client, private_pdf, settings):
    settings.USE_X_ACCEL = True
    response = api_client.get(signed(client, private_pdf)["url"])
    assert response.status_code == 200 and response.content == b""
    assert response["X-Accel-Redirect"] == f"/media/private/{private_pdf.file}"
    assert response["Content-Type"] == "application/pdf"


def test_expired_link_is_410(client, api_client, private_pdf):
    url = signed(client, private_pdf)["url"]
    with freeze_time(timezone.now() + timedelta(seconds=601)):
        response = api_client.get(url)
    assert response.status_code == 410 and response.json()["code"] == "link_expired"


def test_tampered_link_is_403(client, api_client, private_pdf):
    url = signed(client, private_pdf)["url"]
    token = url.rstrip("/").rsplit("/", 1)[-1]
    forged = token[:-3] + ("aaa" if not token.endswith("aaa") else "bbb")
    response = api_client.get(url.replace(token, forged))
    assert response.status_code == 403 and response.json()["code"] == "signature_invalid"


def test_a_token_from_another_salt_is_refused(api_client, private_pdf):
    token = documents_signer().sign_object({"a": str(private_pdf.uid), "c": private_pdf.checksum_sha256[:16], "v": "o"})
    assert api_client.get(f"/api/v1/media/download/{token}/").status_code == 403


def test_deleted_asset_link_is_404(client, api_client, private_pdf):
    url = signed(client, private_pdf)["url"]
    private_pdf.soft_delete()
    assert api_client.get(url).status_code == 404


def test_thumbnail_variant(client, api_client, user, django_capture_on_commit_callbacks):
    with django_capture_on_commit_callbacks(execute=True):
        photo = stored_asset(user, data=files.jpeg(size=(1000, 500)), visibility="PRIVATE", kind="PHOTO")
    body = signed(client, photo)
    response = api_client.get(body["thumbnail_url"])
    assert response.status_code == 200 and response["Content-Type"] == "image/webp"
    assert b"".join(response.streaming_content)[:4] == b"RIFF"


def test_private_files_are_never_on_a_public_url(client, private_pdf, media_roots):
    detail = client.get(f"/api/v1/media/{private_pdf.uid}/").json()
    assert detail["url"] is None and detail["thumbnail_url"] is None and private_pdf.cdn_url == ""
    assert private_pdf.file not in str(detail)
    assert (media_roots / "private" / private_pdf.file).exists()
    assert not (media_roots / "public" / private_pdf.file).exists()
    # Nothing is routed under the public media prefix outside DEBUG.
    assert client.get(f"/media/public/{private_pdf.file}").status_code == 404
    assert client.get(f"/media/private/{private_pdf.file}").status_code == 404
