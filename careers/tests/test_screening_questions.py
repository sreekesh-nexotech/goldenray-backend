"""Yes/No screening questions: authored on a posting, served on its public page, answered on the application form."""

import json

import pytest
from django.core.files.uploadedfile import SimpleUploadedFile

from careers.models import JobApplication
from careers.services import screening
from careers.tests.factories import DepartmentFactory, JobPositionFactory
from core.errors import DomainError
from media.models import MediaAsset
from media.tests import files

pytestmark = pytest.mark.django_db
POSITIONS = "/api/v1/careers/positions/"
PUBLIC = "/api/public/v1/job-positions/"
APPLY = "/api/public/v1/job-applications/"
QUESTIONS = [
    {"key": "license", "question": "Do you hold a valid two-wheeler licence?", "required": True},
    {"key": "relocate", "question": "Are you willing to relocate?", "required": False},
]


def application(position, **overrides):
    data = {
        "position": position.title,
        "position_id": str(position.uid),
        "full_name": "Anu Thomas",
        "email": "anu@example.com",
        "phone": "9847012345",
        "location": "Alappuzha",
        "linkedin": "linkedin.com/in/anu",
        "declaration_accepted": "true",
        "resume": SimpleUploadedFile("cv.pdf", files.pdf(), content_type="application/pdf"),
    }
    data.update(overrides)
    return data


class TestAuthoring:
    def test_create_assigns_keys_and_defaults_required(self, hr_client):
        body = {
            "title": "Solar Technician",
            "department": str(DepartmentFactory().uid),
            "location": "Alappuzha",
            "description": "Install systems.",
            "screening_questions": [{"question": "  Can you work at heights?  "}, {"key": "own-tools", "question": "Do you own tools?", "required": False}],
        }
        response = hr_client.post(POSITIONS, body, format="json")
        assert response.status_code == 201, response.json()
        questions = response.json()["screening_questions"]
        assert questions[0]["question"] == "Can you work at heights?" and questions[0]["required"] is True and questions[0]["key"].startswith("q")
        assert questions[1] == {"key": "own-tools", "question": "Do you own tools?", "required": False}

    def test_update_keeps_keys_sent_back(self, hr_client):
        position = JobPositionFactory(screening_questions=QUESTIONS)
        edited = [{**QUESTIONS[0], "question": "Do you hold a valid driving licence?"}]
        response = hr_client.patch(f"{POSITIONS}{position.uid}/", {"screening_questions": edited, "expected_version": 1}, format="json")
        assert response.status_code == 200 and response.json()["screening_questions"] == edited
        assert response.json()["version"] == 2

    @pytest.mark.parametrize(
        "questions",
        [
            [{"question": ""}],
            [{"key": "a", "question": "x"}, {"key": "a", "question": "y"}],
            [{"key": "Bad Key", "question": "x"}],
            [{"question": f"q{i}"} for i in range(screening.MAX_QUESTIONS + 1)],
        ],
    )
    def test_invalid_questions_are_400(self, hr_client, questions):
        position = JobPositionFactory()
        response = hr_client.patch(f"{POSITIONS}{position.uid}/", {"screening_questions": questions}, format="json")
        assert response.status_code == 400 and response.json()["code"] == "validation_error" and "screening_questions" in response.json()["errors"]


class TestPublicPage:
    def test_detail_lists_the_questions(self, api_client):
        JobPositionFactory(published=True, slug="technician", screening_questions=QUESTIONS)
        JobPositionFactory(published=True, slug="plain")
        assert api_client.get(f"{PUBLIC}technician/").json()["data"]["screening_questions"] == QUESTIONS
        assert api_client.get(f"{PUBLIC}plain/").json()["data"]["screening_questions"] == []


class TestApplying:
    def test_answers_are_stored_with_the_question_text(self, api_client, hr_client):
        position = JobPositionFactory(published=True, screening_questions=QUESTIONS)
        response = api_client.post(APPLY, application(position, screening_answers=json.dumps({"license": "yes", "relocate": False, "unknown": True})), format="multipart")
        assert response.status_code == 201, response.json()
        stored = JobApplication.objects.get(uid=response.json()["uid"])
        assert stored.screening_answers == [
            {"key": "license", "question": QUESTIONS[0]["question"], "answer": True},
            {"key": "relocate", "question": QUESTIONS[1]["question"], "answer": False},
        ]
        detail = hr_client.get(f"/api/v1/careers/applications/{stored.uid}/").json()
        assert detail["screening_answers"] == stored.screening_answers

    def test_optional_question_may_be_skipped(self, api_client):
        position = JobPositionFactory(published=True, screening_questions=QUESTIONS)
        response = api_client.post(APPLY, application(position, screening_answers=json.dumps({"license": True})), format="multipart")
        assert response.status_code == 201
        assert [a["key"] for a in JobApplication.objects.get().screening_answers] == ["license"]

    def test_missing_required_answer_is_400_and_stores_no_file(self, api_client):
        position = JobPositionFactory(published=True, screening_questions=QUESTIONS)
        response = api_client.post(APPLY, application(position, screening_answers=json.dumps({"relocate": True})), format="multipart")
        assert response.status_code == 400 and response.json()["errors"]["screening_answers"] == [f"Please answer: {QUESTIONS[0]['question']}"]
        assert not JobApplication.objects.exists() and not MediaAsset.all_objects.exists()

    @pytest.mark.parametrize("value", ["not json", "[true]"])
    def test_malformed_answers_are_400(self, api_client, value):
        position = JobPositionFactory(published=True, screening_questions=QUESTIONS)
        response = api_client.post(APPLY, application(position, screening_answers=value), format="multipart")
        assert response.status_code == 400 and "screening_answers" in response.json()["errors"]

    def test_general_application_has_no_questions(self, api_client):
        data = application(JobPositionFactory(published=True), screening_answers=json.dumps({"license": True}))
        del data["position_id"]
        assert api_client.post(APPLY, data, format="multipart").status_code == 201
        assert JobApplication.objects.get().screening_answers == []


class TestService:
    def test_answers_accept_yes_no_words(self):
        assert [a["answer"] for a in screening.answers_for(QUESTIONS, {"license": "No", "relocate": "1"})] == [False, True]

    def test_a_non_yes_no_value_does_not_answer_a_required_question(self):
        with pytest.raises(DomainError) as raised:
            screening.answers_for(QUESTIONS, {"license": "maybe"})
        assert raised.value.code == "validation_error"
