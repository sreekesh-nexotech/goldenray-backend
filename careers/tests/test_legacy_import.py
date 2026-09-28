"""careers.services.legacy_import — idempotent, traceable imports of the CMS careers tables and the backend queue.

Fixtures were exported read-only from the legacy sources (``legacy/cms_*.json``) and from the private write-path
parity run (``legacy/backend_applications.json`` + ``legacy/media/``; personal data masked).
"""

import copy
import hashlib
import json
from pathlib import Path

import pytest

from audit.models import AuditLog
from careers.models import JobApplication, JobApplicationEvent, JobApplicationNote, JobPosition
from careers.services import legacy_import
from core.models import LegacyMap
from media.models import MediaAsset

pytestmark = pytest.mark.django_db
LEGACY = Path(__file__).resolve().parent / "legacy"
CMS_ROWS = json.loads((LEGACY / "cms_enriched.json").read_text())
BACKEND_ROWS = json.loads((LEGACY / "backend_applications.json").read_text())


def read_file(path: str):
    target = LEGACY / "media" / path
    return target.read_bytes() if target.is_file() else None


def import_cms():
    return legacy_import.import_departments(CMS_ROWS["careers_department"]), legacy_import.import_positions(CMS_ROWS["careers_job_position"])


def import_backend():
    return (
        legacy_import.import_applications(BACKEND_ROWS["job_application"], read_file=read_file),
        legacy_import.import_application_notes(BACKEND_ROWS["job_application_note"]),
        legacy_import.import_application_events(BACKEND_ROWS["job_application_event"]),
    )


class TestCms:
    def test_departments_and_positions(self):
        departments, positions = import_cms()
        assert departments == {"created": 5, "updated": 0, "skipped": 0, "violations": []}
        assert positions == {"created": 10, "updated": 0, "skipped": 0, "violations": []}
        designer = JobPosition.objects.get(slug="senior-solar-designer")
        assert designer.status == "PUBLISHED" and designer.employment_type == "part_time" and designer.noindex is True
        assert designer.schema_extra["industry"] == "Solar" and designer.department.slug == "engineering"
        source = next(row for row in CMS_ROWS["careers_job_position"] if row["slug"] == "senior-solar-designer")
        assert designer.created_at.isoformat() == source["created_at"] and designer.updated_at.isoformat() == source["updated_at"]
        assert {row["status"] for row in JobPosition.objects.values("status")} == {"PUBLISHED", "DRAFT", "CLOSED", "ARCHIVED"}
        assert LegacyMap.objects.filter(source_system="CMS", source_table="careers_job_position").count() == 10
        entry = AuditLog.objects.filter(action="careers.legacy_imported", object_type="careers.jobposition").get()
        assert entry.after["created"] == 10 and entry.after["checksum"] == legacy_import.checksum(CMS_ROWS["careers_job_position"])

    def test_rerun_updates_changed_rows_only(self):
        import_cms()
        rows = copy.deepcopy(CMS_ROWS["careers_job_position"])
        rows[0]["title"] = "Solar Installation Engineer (Senior)"
        result = legacy_import.import_positions(rows)
        assert result["created"] == 0 and result["updated"] == 1 and result["skipped"] == 9
        position = JobPosition.objects.get(slug=rows[0]["slug"])
        assert position.title == "Solar Installation Engineer (Senior)" and position.version == 2
        assert JobPosition.all_objects.count() == 10

    def test_rows_deleted_in_the_platform_stay_deleted(self):
        import_cms()
        JobPosition.objects.get(slug="archived-role").soft_delete()
        result = legacy_import.import_positions(CMS_ROWS["careers_job_position"])
        assert result["skipped"] == 10 and not JobPosition.objects.filter(slug="archived-role").exists()

    def test_violations(self):
        legacy_import.import_departments(CMS_ROWS["careers_department"])
        rows = copy.deepcopy(CMS_ROWS["careers_job_position"][:3])
        rows[0]["status"] = "review"
        rows[1]["department_id"] = 999
        rows[2]["employment_type"] = "freelance"
        extra = dict(CMS_ROWS["careers_job_position"][3], id=900, og_image_id=77)
        clash = dict(CMS_ROWS["careers_job_position"][4], id=901)
        legacy_import.import_positions([CMS_ROWS["careers_job_position"][4]])
        result = legacy_import.import_positions([*rows, extra, clash])
        fields = [violation["field"] for violation in result["violations"]]
        assert fields == ["status", "department_id", "employment_type", "og_image_id", "slug"]
        assert result["created"] == 1  # the posting whose OG image was not imported is imported without it
        bad_department = legacy_import.import_departments([{"id": 50, "name": "", "slug": ""}])
        assert bad_department["violations"][0]["field"] == "name"

    def test_users_and_og_images_resolve_through_the_maps(self, make_user):
        from media.tests.factories import MediaAssetFactory

        legacy_import.import_departments(CMS_ROWS["careers_department"])
        user = make_user()
        image = MediaAssetFactory()
        LegacyMap.objects.create(source_system="CMS", source_table="accounts_admin_user", source_id="3", target_table="accounts_user", target_id=user.pk)
        LegacyMap.objects.create(source_system="CMS", source_table="media_asset", source_id="8", target_table="media_asset", target_id=image.pk)
        row = dict(CMS_ROWS["careers_job_position"][0], created_by_id=3, updated_by_id=3, og_image_id=8)
        legacy_import.import_positions([row])
        position = JobPosition.objects.get(slug=row["slug"])
        assert position.created_by == user and position.og_image == image


class TestBackend:
    def test_applications_notes_and_events(self):
        legacy_import.import_departments(CMS_ROWS["careers_department"])
        legacy_import.import_positions(CMS_ROWS["careers_job_position"])
        applications, notes, events = import_backend()
        assert applications["created"] == 9 and applications["updated"] == 0
        assert [(violation["source_id"], violation["field"]) for violation in applications["violations"]] == [("8", "resume")]
        assert notes == {"created": 2, "updated": 0, "skipped": 0, "violations": []}
        assert events["created"] == 19 and events["violations"] == []

        first = JobApplication.objects.get(pk=legacy_import.mapped_id("BACKEND", "job_application", 1))
        assert first.status == "OFFERED" and first.source == "IMPORT" and first.name == "Candidate 1" and first.phone_e164 == "+919800000001"
        assert first.position.slug == "solar-installation-engineer" and first.position_label == "General application" and first.position_title == "Solar Installation Engineer"
        assert first.resume.visibility == MediaAsset.Visibility.PRIVATE and first.resume.original_filename == "Candidate_1_Resume.pdf"
        assert first.resume.checksum_sha256 == hashlib.sha256(read_file("job_applications/resumes/cv.pdf")).hexdigest()
        source = next(row for row in BACKEND_ROWS["job_application"] if row["id"] == 1)
        assert first.created_at.isoformat() == source["created_at"]
        assert list(first.events.order_by("created_at", "id").values_list("kind", "to_status")) == [
            ("RECEIVED", ""),
            ("STATUS", "SCREENING"),
            ("NOTE", ""),
            ("ASSIGNED", ""),
            ("STATUS", "INTERVIEW"),
            ("STATUS", "OFFERED"),
        ]
        assert first.notes.get().author_name == "hr.parity"

        archived = JobApplication.all_objects.get(pk=legacy_import.mapped_id("BACKEND", "job_application", 2))
        assert archived.status == "REJECTED" and archived.deleted_at is not None
        with_portfolio = JobApplication.objects.get(pk=legacy_import.mapped_id("BACKEND", "job_application", 4))
        assert with_portfolio.resume.mime_type.endswith("wordprocessingml.document") and with_portfolio.portfolio.mime_type == "application/msword"
        refused = JobApplication.objects.get(pk=legacy_import.mapped_id("BACKEND", "job_application", 8))
        assert refused.resume is None  # the legacy "PDF" was not a PDF: imported without it, reported
        closed = JobApplication.objects.get(pk=legacy_import.mapped_id("BACKEND", "job_application", 9))
        assert closed.position.status == "CLOSED"

    def test_rerun_is_idempotent_and_keeps_files(self):
        legacy_import.import_departments(CMS_ROWS["careers_department"])
        legacy_import.import_positions(CMS_ROWS["careers_job_position"])
        import_backend()
        assets = MediaAsset.objects.count()
        applications, notes, events = import_backend()
        assert applications["created"] == applications["updated"] == 0 and applications["skipped"] == 9
        assert notes["skipped"] == 2 and events["skipped"] == 19
        assert MediaAsset.objects.count() == assets and JobApplication.all_objects.count() == 9
        assert JobApplicationNote.objects.count() == 2 and JobApplicationEvent.objects.count() == 19

    def test_an_application_archived_in_the_platform_stays_archived(self, make_user):
        """The module contract: a row deleted in the platform after the import stays deleted (the re-import un-archived it)."""
        from careers.services.applications import archive_application, restore_application

        rows = copy.deepcopy([row for row in BACKEND_ROWS["job_application"] if not row["archived_at"]][:2])
        legacy_import.import_applications(rows, read_file=read_file)
        hr = make_user(grants={"applications": "*"})
        archived, restored = (JobApplication.objects.get(pk=legacy_import.mapped_id("BACKEND", "job_application", row["id"])) for row in rows)
        archive_application(archived, user=hr)
        archive_application(restored, user=hr)
        restore_application(JobApplication.all_objects.get(pk=restored.pk), user=hr)
        rows[1]["archived_at"] = "2026-09-01T10:00:00+00:00"  # archived in the legacy queue meanwhile
        again = legacy_import.import_applications(rows, read_file=read_file)
        assert JobApplication.all_objects.get(pk=archived.pk).deleted_at is not None and again["skipped"] >= 1
        assert JobApplication.all_objects.get(pk=restored.pk).deleted_at is None  # the platform's own restore wins too

    def test_legacy_archive_and_restore_still_flow_into_untouched_rows(self):
        rows = copy.deepcopy([row for row in BACKEND_ROWS["job_application"] if not row["archived_at"]][:1])
        legacy_import.import_applications(rows, read_file=read_file)
        application = JobApplication.objects.get()
        rows[0]["archived_at"] = "2026-09-01T10:00:00+00:00"
        legacy_import.import_applications(rows, read_file=read_file)
        assert JobApplication.all_objects.get(pk=application.pk).deleted_at is not None
        rows[0]["archived_at"] = None
        legacy_import.import_applications(rows, read_file=read_file)
        assert JobApplication.all_objects.get(pk=application.pk).deleted_at is None

    def test_changed_file_replaces_the_asset(self):
        rows = copy.deepcopy(BACKEND_ROWS["job_application"][:1])
        legacy_import.import_applications(rows, read_file=read_file)
        application = JobApplication.objects.get()
        old = application.resume
        result = legacy_import.import_applications(rows, read_file=lambda path: read_file("job_applications/resumes/cv.docx"))
        application.refresh_from_db()
        assert result["updated"] == 1 and application.resume != old and application.resume.original_filename == "Candidate_1_Resume.docx"
        assert not MediaAsset.objects.filter(pk=old.pk).exists()

    def test_violations(self):
        rows = copy.deepcopy(BACKEND_ROWS["job_application"][:4])
        rows[0]["status"] = "hired"
        rows[1]["phone"] = "12345"
        rows[2]["total_experience"] = "20 years"
        rows[3]["resume"] = "job_applications/resumes/missing.pdf"
        result = legacy_import.import_applications(rows, read_file=read_file)
        assert [(violation["source_id"], violation["field"]) for violation in result["violations"]] == [
            ("1", "status"),
            ("2", "phone"),
            ("3", "position_id"),  # the CMS postings were not imported in this test
            ("3", "total_experience"),
            ("4", "resume"),
        ]
        assert result["created"] == 2
        orphan_note = legacy_import.import_application_notes([{"id": 99, "application_id": 12345, "author": "x", "body": "y", "created_at": None}])
        assert orphan_note["violations"][0]["field"] == "application_id"
        empty_note = legacy_import.import_application_notes([{"id": 98, "application_id": 3, "author": "x", "body": " ", "created_at": None}])
        assert empty_note["violations"][0]["field"] == "body"
        bad_event = legacy_import.import_application_events([{"id": 97, "application_id": 3, "kind": "exploded", "created_at": None}])
        assert bad_event["violations"][0]["field"] == "kind"
