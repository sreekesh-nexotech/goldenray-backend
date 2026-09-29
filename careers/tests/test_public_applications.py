"""POST /api/public/v1/job-applications/ — the legacy form's fields and rules, private resumes, honeypot, idempotency."""

import pytest
from django.core.files.uploadedfile import SimpleUploadedFile

from audit.models import AuditLog
from careers.models import JobApplication, JobApplicationEvent, JobPosition
from careers.tests.factories import JobPositionFactory
from core.models import OutboxEvent
from media.models import MediaAsset
from media.tests import files

pytestmark = pytest.mark.django_db
URL = "/api/public/v1/job-applications/"


def form(**overrides):
    data = {
        "position": "General application",
        "full_name": "  Anu Thomas ",
        "email": " Anu.Thomas@Example.com ",
        "phone": "+91 98470-12345",
        "location": "Alappuzha",
        "linkedin": "linkedin.com/in/anu-thomas",
        "portfolio_website": "anuthomas.dev",
        "total_experience": "1–3 years",
        "expected_salary": "₹3–5 LPA",
        "notice_period": "1 month",
        "heard_about_us": "LinkedIn",
        "cover_note": "I like solar.",
        "declaration_accepted": "true",
        "website": "",
        "resume": SimpleUploadedFile("cv.pdf", files.pdf(), content_type="application/pdf"),
    }
    data.update(overrides)
    return {key: value for key, value in data.items() if value is not None}


def post(client, data, **headers):
    return client.post(URL, data, format="multipart", **headers)


class TestHappyPath:
    def test_general_application_is_stored_with_a_private_resume(self, api_client):
        response = post(api_client, form())
        assert response.status_code == 201, response.json()
        body = response.json()
        assert set(body) == {"uid", "display_position", "created_at", "message"} and body["display_position"] == "General application"
        application = JobApplication.objects.get(uid=body["uid"])
        assert application.name == "Anu Thomas" and application.email == "anu.thomas@example.com" and application.phone_e164 == "+919847012345"
        assert application.linkedin == "https://linkedin.com/in/anu-thomas" and application.portfolio_website == "https://anuthomas.dev"
        assert application.cover_letter == "I like solar." and application.position is None and application.status == "NEW" and application.source == "WEBSITE"
        assert application.ip == "127.0.0.1"
        resume = application.resume
        assert resume.visibility == MediaAsset.Visibility.PRIVATE and resume.kind == "RESUME" and resume.folder == "careers/applications"
        assert resume.original_filename == "Anu_Thomas_Resume.pdf" and resume.mime_type == "application/pdf" and resume.cdn_url == ""
        assert list(application.events.values_list("kind", flat=True)) == [JobApplicationEvent.Kind.RECEIVED]
        entry = AuditLog.objects.get(action="careers.application_received")
        assert entry.actor is None and entry.actor_kind == "SYSTEM"
        assert OutboxEvent.objects.get(event_type="careers.application_received").payload["application_uid"] == body["uid"]

    def test_posting_application_snapshots_the_posting(self, api_client):
        position = JobPositionFactory(published=True, title="Solar Installation Engineer", department__name="Engineering")
        response = post(api_client, form(position="Solar Installation Engineer", position_id=str(position.uid), position_title="tampered", department_name="tampered"))
        assert response.status_code == 201, response.json()
        application = JobApplication.objects.get(uid=response.json()["uid"])
        assert application.position == position and application.position_title == "Solar Installation Engineer" and application.department_name == "Engineering"

    def test_portfolio_docx_is_accepted(self, api_client):
        response = post(api_client, form(portfolio_file=SimpleUploadedFile("work.docx", files.docx())))
        assert response.status_code == 201
        application = JobApplication.objects.get(uid=response.json()["uid"])
        assert application.portfolio.original_filename == "Anu_Thomas_Portfolio.docx" and application.portfolio.visibility == "PRIVATE"

    def test_resumes_never_appear_in_the_media_library(self, api_client, auth_client, make_user):
        post(api_client, form())
        library = auth_client(make_user(grants={"media": ["view"]})).get("/api/v1/media/").json()
        assert library["count"] == 0

    def test_notification_email_when_the_company_asks_for_it(self, api_client, drain_outbox, mailoutbox, django_capture_on_commit_callbacks):
        from company.tests.factories import CompanyProfileFactory

        CompanyProfileFactory(notify_on_new_application=True, application_notification_emails=["hr@flarize.com"])
        post(api_client, form())
        with django_capture_on_commit_callbacks(execute=True):
            drain_outbox()
        assert len(mailoutbox) == 1 and mailoutbox[0].to == ["hr@flarize.com"] and "General application" in mailoutbox[0].subject

    def test_no_email_when_notifications_are_off(self, api_client, drain_outbox, mailoutbox, django_capture_on_commit_callbacks):
        from company.tests.factories import CompanyProfileFactory

        CompanyProfileFactory(notify_on_new_application=False, application_notification_emails=["hr@flarize.com"])
        post(api_client, form())
        with django_capture_on_commit_callbacks(execute=True):
            drain_outbox()
        assert mailoutbox == []


class TestLegacyValidation:
    """The legacy serializer's rules, error keyed by the legacy field names."""

    @pytest.mark.parametrize(
        "overrides,field",
        [
            ({"full_name": "   "}, "full_name"),
            ({"email": "not-an-email"}, "email"),
            ({"phone": "12345"}, "phone"),
            ({"phone": "5847012345"}, "phone"),
            ({"location": ""}, "location"),
            ({"linkedin": "https://example.com/anu"}, "linkedin"),
            ({"total_experience": "10 years"}, "total_experience"),
            ({"expected_salary": "a lot"}, "expected_salary"),
            ({"cover_note": "x" * 3001}, "cover_note"),
            ({"declaration_accepted": "false"}, "declaration_accepted"),
            ({"resume": None}, "resume"),
            ({"resume": SimpleUploadedFile("cv.txt", b"hello")}, "resume"),
            ({"position": "x" * 201}, "position"),
            ({"position_id": "12"}, "position_id"),
            ({"availability": "x" * 33}, "availability"),
        ],
    )
    def test_field_errors(self, api_client, overrides, field):
        response = post(api_client, form(**overrides))
        assert response.status_code == 400, response.json()
        body = response.json()
        assert body["code"] == "validation_error" and field in body["errors"], body
        assert not JobApplication.all_objects.exists() and not MediaAsset.all_objects.exists()

    def test_blank_optional_form_fields_mean_omitted(self, api_client):
        """Multipart forms send "" for untouched inputs; like the legacy form, that falls back to the default."""
        response = post(api_client, form(position="", total_experience=""))
        assert response.status_code == 201 and response.json()["display_position"] == "General application"

    def test_phone_normalisation_accepts_91_prefix(self, api_client):
        assert post(api_client, form(phone="919847012345")).status_code == 201

    @pytest.mark.parametrize("field,value", [("linkedin", "linkedin.com/in/" + "a" * 280), ("portfolio_website", "example.com/" + "b" * 285)])
    def test_url_longer_than_the_column_once_https_is_added_is_a_400_not_a_500(self, api_client, field, value):
        """Within the 300-character input limit, but 300+ once ``https://`` is prepended (the legacy form answered 500)."""
        response = post(api_client, form(**{field: value}))
        assert response.status_code == 400, response.json()
        assert response.json()["errors"][field] == ["Ensure this field has no more than 300 characters."]
        assert not JobApplication.all_objects.exists() and not MediaAsset.all_objects.exists()
        exactly = post(api_client, form(**{field: "https://" + value[: 300 - len("https://")]}))
        assert exactly.status_code == 201, exactly.json()

    def test_file_over_10_mb_is_refused_with_the_legacy_message(self, api_client):
        big = SimpleUploadedFile("cv.pdf", files.pdf() + b"0" * (10 * 1024 * 1024))
        response = post(api_client, form(resume=big))
        assert response.status_code == 400 and response.json()["errors"]["resume"] == ["File must be under 10MB."]

    def test_content_is_sniffed(self, api_client):
        fake = SimpleUploadedFile("cv.pdf", b"not really a pdf at all")
        response = post(api_client, form(resume=fake))
        assert response.status_code == 400 and response.json()["code"] == "unsupported_file_type" and "resume" in response.json()["errors"]
        assert not JobApplication.all_objects.exists()

    def test_bad_portfolio_discards_the_stored_resume(self, api_client):
        response = post(api_client, form(portfolio_file=SimpleUploadedFile("work.pdf", files.jpeg())))
        assert response.status_code == 400 and "portfolio_file" in response.json()["errors"]
        assert not JobApplication.all_objects.exists()
        assert MediaAsset.objects.count() == 0 and MediaAsset.all_objects.count() == 1  # the resume was stored, then discarded

    def test_honeypot(self, api_client):
        response = post(api_client, form(website="http://spam.example"))
        assert response.status_code == 400 and response.json()["errors"]["non_field_errors"] == ["Invalid submission."]
        assert not JobApplication.all_objects.exists()

    @pytest.mark.parametrize("status", [JobPosition.Status.DRAFT, JobPosition.Status.CLOSED, JobPosition.Status.ARCHIVED])
    def test_only_published_postings_accept_applications(self, api_client, status):
        position = JobPositionFactory(status=status)
        response = post(api_client, form(position_id=str(position.uid)))
        assert response.status_code == 400 and response.json()["code"] == "position_not_open" and "position_id" in response.json()["errors"]
        assert not MediaAsset.all_objects.exists()


class TestProtections:
    def test_idempotency_key_replays_the_first_response(self, api_client):
        first = post(api_client, form(), HTTP_IDEMPOTENCY_KEY="submit-0001")
        second = post(api_client, form(), HTTP_IDEMPOTENCY_KEY="submit-0001")
        assert first.status_code == second.status_code == 201
        assert second["Idempotent-Replayed"] == "true" and second.json()["uid"] == first.json()["uid"]
        assert JobApplication.objects.count() == 1
        reused = post(api_client, form(full_name="Someone Else"), HTTP_IDEMPOTENCY_KEY="submit-0001")
        assert reused.status_code == 422 and reused.json()["code"] == "idempotency_key_reused"

    def test_throttle_scope_is_public_write(self, api_client, settings):
        from careers.views.public import PublicJobApplicationView

        view = PublicJobApplicationView()
        assert view.authentication_classes == [] and view.get_throttle_scope(type("R", (), {"method": "POST"})()) == "public_write"
        settings.REST_FRAMEWORK = {**settings.REST_FRAMEWORK, "DEFAULT_THROTTLE_RATES": {**settings.REST_FRAMEWORK["DEFAULT_THROTTLE_RATES"], "public_write": "2/min"}}
        codes = [post(api_client, form()).status_code for _ in range(3)]
        assert codes == [201, 201, 429]

    def test_json_body_is_refused(self, api_client):
        response = api_client.post(URL, {"full_name": "x"}, format="json")
        assert response.status_code == 415
