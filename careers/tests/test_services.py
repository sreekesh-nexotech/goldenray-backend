"""Service-level DomainErrors and helpers the HTTP tests cannot reach directly."""

import io

import pytest

from careers.models import JobApplication, JobPosition
from careers.services import applications, departments, positions, validation
from careers.tests.factories import DepartmentFactory, JobApplicationFactory, JobPositionFactory
from core.errors import DomainError, NotFound
from media.tests import files

pytestmark = pytest.mark.django_db


def test_department_slug_cannot_be_derived(make_user):
    with pytest.raises(DomainError) as caught:
        departments.create_department(user=make_user(), data={"name": "!!!"})
    assert caught.value.code == "validation_error" and "slug" in caught.value.errors


def test_blank_slug_on_update_is_rederived(make_user):
    department = DepartmentFactory(name="Field Ops", slug="old")
    updated = departments.update_department(department, user=make_user(), data={"slug": ""})
    assert updated.slug == "field-ops"


def test_position_on_a_deleted_department(make_user):
    department = DepartmentFactory()
    department.soft_delete()
    with pytest.raises(DomainError) as caught:
        positions.create_position(user=make_user(), data={"title": "X", "department": department, "location": "Kochi"})
    assert "department" in caught.value.errors


def test_position_slug_cannot_be_derived(make_user):
    with pytest.raises(DomainError) as caught:
        positions.create_position(user=make_user(), data={"title": "???", "department": DepartmentFactory(), "location": "Kochi"})
    assert "slug" in caught.value.errors


def test_blank_position_slug_on_update_is_rederived(make_user):
    position = JobPositionFactory(title="Store Keeper", slug="old")
    assert positions.update_position(position, user=make_user(), data={"slug": ""}).slug == "store-keeper"


def test_publish_errors_list_everything_missing():
    position = JobPosition(title=" ", location="", description="")
    assert positions.publish_errors(position) == ["A job title is required.", "Choose a department.", "A location is required.", "A job description is required."]


def test_assign_to_a_deleted_position(make_user):
    application = JobApplicationFactory()
    gone = JobPositionFactory()
    gone.soft_delete()
    with pytest.raises(DomainError) as caught:
        applications.assign(application, user=make_user(), position=gone)
    assert "position_uid" in caught.value.errors


def test_assign_without_changes_is_a_no_op(make_user):
    application = JobApplicationFactory()
    same = applications.assign(application, user=make_user(), position=application.position)
    assert same.version == 1


def test_note_needs_a_body(make_user):
    with pytest.raises(DomainError):
        applications.add_note(JobApplicationFactory(), user=make_user(), body="  ")


def test_download_link_unknown_kind():
    with pytest.raises(NotFound):
        applications.download_link(JobApplicationFactory(), "passport", version="v1")


def test_submission_to_a_missing_posting():
    with pytest.raises(DomainError) as caught:
        applications.open_position("00000000-0000-0000-0000-000000000000")
    assert caught.value.code == "position_not_open"


def test_store_candidate_file_keys_errors_by_form_field():
    buffer = io.BytesIO(files.jpeg())
    buffer.name = "cv.pdf"
    buffer.size = len(buffer.getvalue())
    with pytest.raises(DomainError) as caught:
        applications.store_candidate_file(buffer, candidate="Anu", kind="portfolio")
    assert caught.value.code == "unsupported_file_type" and list(caught.value.errors) == ["portfolio_file"]


def test_extension_falls_back_to_the_client_name_for_unrecognised_content():
    buffer = io.BytesIO(b"garbage")
    assert applications._extension(buffer, "CV.DOCX") == ".docx" and buffer.tell() == 0
    assert applications._extension(io.BytesIO(b"garbage"), "noextension") == ""


def test_validation_helpers():
    assert validation.indian_mobile("+91 (984) 701-2345") == "9847012345"
    assert validation.indian_mobile("1234567890") is None
    assert validation.linkedin_url("https://www.linkedin.com/in/x") == "https://www.linkedin.com/in/x"
    assert validation.linkedin_url("linkedin.com.evil.example/in/x") is None
    assert validation.website_url("  ") == "" and validation.website_url("http://a.example") == "http://a.example"
    assert validation.upload_error("cv.PDF", 10) is None and validation.upload_error("cv", 10) == "Only PDF or Word documents are allowed."
    assert validation.download_name("  ", "Resume", ".pdf") == "application_Resume.pdf"


def test_display_position_and_transitions():
    application = JobApplication(position_label="General application", position_title="", status="REJECTED")
    assert application.display_position == "General application" and application.allowed_transitions() == ["SCREENING"]
    assert str(application).endswith("General application")


def test_posting_unpublished_while_files_were_stored_is_refused():
    position = JobPositionFactory(published=True)
    JobPosition.objects.filter(pk=position.pk).update(status=JobPosition.Status.CLOSED)
    with pytest.raises(DomainError) as caught:
        applications._create_application(data={"name": "Anu"}, position=position, resume=None, portfolio=None, ip=None)
    assert caught.value.code == "position_not_open" and not JobApplication.all_objects.exists()
