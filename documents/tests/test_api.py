"""documents/jobs/<uid>/, documents/jobs/<uid>/download-url/ and the single-use documents/download/<token>/."""

import uuid
from datetime import timedelta

import pytest
from django.utils import timezone
from freezegun import freeze_time

from audit.models import AuditLog
from documents import access
from documents.models import DocumentDownload, RenderJob
from documents.services import jobs
from documents.tests.factories import render
from media.services.signing import _signer as media_signer

pytestmark = pytest.mark.django_db


def done_job(user, **kwargs):
    job = render(user, **kwargs)
    jobs.run_job(str(job.uid))
    job.refresh_from_db()
    return job


@pytest.fixture
def pricing_user(make_user):
    return make_user(grants={"pricing": ["view"]})


def job_url(job, suffix=""):
    return f"/api/v1/documents/jobs/{job.uid}/{suffix}"


class TestJobStatus:
    def test_anonymous_is_401(self, api_client, pricing_user):
        job = render(pricing_user)
        assert api_client.get(job_url(job)).status_code == 401
        assert api_client.post(job_url(job, "download-url/")).status_code == 401

    def test_needs_the_kind_permission(self, auth_client, make_user, pricing_user):
        job = render(pricing_user)
        client = auth_client(make_user(grants={"media": "*"}))
        assert client.get(job_url(job)).status_code == 403
        assert client.post(job_url(job, "download-url/")).status_code == 403

    def test_shape(self, auth_client, pricing_user):
        job = done_job(pricing_user)
        body = auth_client(pricing_user).get(job_url(job)).json()
        assert body["status"] == "DONE" and body["download_available"] is True and body["page_count"] == 1 and body["error"] is None
        assert body["requested_by"] == str(pricing_user.uid) and body["kind"] == "PUBLISH_REPORT"
        assert "payload" not in body and "file" not in body and "asset" not in body

    def test_failed_job_shows_its_error(self, auth_client, pricing_user):
        job = render(pricing_user)
        jobs.fail(job.uid, "RenderError: broken")
        assert auth_client(pricing_user).get(job_url(job)).json()["error"] == "RenderError: broken"

    def test_unknown_job_is_404(self, auth_client, pricing_user):
        assert auth_client(pricing_user).get(f"/api/v1/documents/jobs/{uuid.uuid4()}/").status_code == 404


class TestRecordScope:
    """A document inherits the visibility of its record: owned scope sees only the jobs it requested."""

    def test_owned_scope(self, auth_client, make_user):
        owner = make_user(grants={"quotations": ["view"]}, scopes={"quotations": "owned"})
        colleague = make_user(grants={"quotations": ["view"]}, scopes={"quotations": "owned"})
        manager = make_user(grants={"quotations": ["view"]}, scopes={"quotations": "all"})
        job = RenderJob.objects.create(kind="QUOTATION", object_type="quotations.quotationversion", object_uid=uuid.uuid4(), language="en", payload={}, payload_sha256="0" * 64, requested_by=owner)
        assert auth_client(owner).get(job_url(job)).status_code == 200
        assert auth_client(colleague).get(job_url(job)).status_code == 404
        assert auth_client(manager).get(job_url(job)).status_code == 200

    def test_registered_visibility_rule(self, auth_client, make_user):
        visible_uid = uuid.uuid4()
        access.register("quotations.quotationversion", module="quotations", action="view", visible=lambda user, object_uid: object_uid == visible_uid)
        try:
            viewer = make_user(grants={"quotations": ["view"]}, scopes={"quotations": "all"})
            shown = RenderJob.objects.create(kind="QUOTATION", object_type="quotations.quotationversion", object_uid=visible_uid, language="en", payload={}, payload_sha256="0" * 64)
            hidden = RenderJob.objects.create(kind="QUOTATION", object_type="quotations.quotationversion", object_uid=uuid.uuid4(), language="en", payload={}, payload_sha256="0" * 64)
            client = auth_client(viewer)
            assert client.get(job_url(shown)).status_code == 200
            assert client.get(job_url(hidden)).status_code == 404
        finally:
            access.unregister("quotations.quotationversion")

    def test_register_refuses_unknown_permissions(self):
        with pytest.raises(ValueError):
            access.register("x.y", module="quotations", action="fly")


class TestDownloads:
    def test_single_use_download(self, auth_client, api_client, pricing_user, document_storage):
        job = done_job(pricing_user)
        response = auth_client(pricing_user).post(job_url(job, "download-url/"))
        assert response.status_code == 201
        body = response.json()
        assert body["url"].startswith("http://testserver/api/v1/documents/download/")
        assert timedelta(minutes=9) < timezone.datetime.fromisoformat(body["expires_at"]) - timezone.now() <= timedelta(minutes=10)
        first = api_client.get(body["url"], HTTP_USER_AGENT="Studio/1.0", REMOTE_ADDR="10.1.2.3")
        assert first.status_code == 200 and first["Content-Type"] == "application/pdf"
        assert b"".join(first.streaming_content) == (document_storage / "private" / job.file).read_bytes()
        assert first["Content-Disposition"].startswith(f'attachment; filename="publish_report-{job.object_uid}-en.pdf"')
        assert first["Cache-Control"] == "private, no-store"
        second = api_client.get(body["url"])
        assert second.status_code == 410 and second.json()["code"] == "link_used"
        download = DocumentDownload.objects.get(job=job)
        assert download.used_at and download.used_ip == "10.1.2.3" and download.used_user_agent == "Studio/1.0" and download.issued_to == pricing_user
        assert AuditLog.objects.filter(action="documents.download_issued", object_uid=job.uid).exists()
        assert AuditLog.objects.filter(action="documents.downloaded", object_uid=job.uid).count() == 1

    def test_each_request_issues_a_new_link(self, auth_client, pricing_user):
        job = done_job(pricing_user)
        client = auth_client(pricing_user)
        assert client.post(job_url(job, "download-url/")).json()["url"] != client.post(job_url(job, "download-url/")).json()["url"]
        assert DocumentDownload.objects.filter(job=job).count() == 2

    def test_not_ready_is_409(self, auth_client, pricing_user):
        job = render(pricing_user)
        response = auth_client(pricing_user).post(job_url(job, "download-url/"))
        assert response.status_code == 409 and response.json()["code"] == "document_not_ready"

    def test_expired_link_is_410(self, auth_client, api_client, pricing_user):
        job = done_job(pricing_user)
        url = auth_client(pricing_user).post(job_url(job, "download-url/")).json()["url"]
        with freeze_time(timezone.now() + timedelta(seconds=601)):
            response = api_client.get(url)
        assert response.status_code == 410 and response.json()["code"] == "link_expired"
        assert DocumentDownload.objects.get(job=job).used_at is None

    def test_tampered_and_foreign_tokens_are_403(self, auth_client, api_client, pricing_user):
        job = done_job(pricing_user)
        url = auth_client(pricing_user).post(job_url(job, "download-url/")).json()["url"]
        token = url.rstrip("/").rsplit("/", 1)[-1]
        assert api_client.get(url.replace(token, token[:-2] + ("xx" if not token.endswith("xx") else "yy"))).status_code == 403
        foreign = media_signer().sign_object({"d": str(DocumentDownload.objects.get(job=job).uid)})
        assert api_client.get(f"/api/v1/documents/download/{foreign}/").status_code == 403
        assert DocumentDownload.objects.get(job=job).used_at is None

    def test_x_accel(self, auth_client, api_client, pricing_user, settings):
        settings.USE_X_ACCEL = True
        job = done_job(pricing_user)
        response = api_client.get(auth_client(pricing_user).post(job_url(job, "download-url/")).json()["url"])
        assert response.status_code == 200 and response.content == b""
        assert response["X-Accel-Redirect"] == f"/media/private/{job.file}"

    def test_rendered_documents_stay_out_of_the_media_library(self, auth_client, make_user, pricing_user):
        job = done_job(pricing_user)
        client = auth_client(make_user(grants={"media": "*"}))
        assert client.get("/api/v1/media/").json()["count"] == 0
        assert client.get(f"/api/v1/media/{job.asset.uid}/signed-url/").status_code == 404
        response = client.delete(f"/api/v1/media/{job.asset.uid}/")
        assert response.status_code == 404
