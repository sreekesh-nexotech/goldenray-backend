"""``careers_job_application`` (+ ``_note``, ``_event``) — PLAN §2.8; legacy ``job_application`` of the main backend.

Every legacy column has a home (docs/decisions/careers-reference.md has the mapping table):

* ``position`` is a real FK now (the CMS and the main backend were two databases). The legacy free-text ``position``
  ("General application" for the talent-pool form) is ``position_label``; ``position_title`` / ``department_name``
  stay as the snapshot taken at submission, so renaming or archiving a posting never rewrites what was applied for.
* ``name`` / ``phone_e164`` / ``cover_letter`` / ``resume`` / ``portfolio`` are the PLAN names of the legacy
  ``full_name`` / ``phone`` / ``cover_note`` / ``resume`` / ``portfolio_file``; the files are PRIVATE media assets.
* the legacy ``archived_at`` is the base model's soft delete (``deleted_at``): archive/restore never lose a row.
* ``status`` follows the PLAN's workflow; the legacy statuses map new→NEW, reviewing→SCREENING,
  interview→INTERVIEW, selected→OFFERED, rejected→REJECTED.
"""

from __future__ import annotations

from django.conf import settings
from django.db import models
from django.db.models import Q
from django.utils import timezone

from careers.models.position import JobPosition
from core.models import BaseModel

GENERAL_APPLICATION = "General application"


class JobApplication(BaseModel):
    class Status(models.TextChoices):
        NEW = "NEW", "New"
        SCREENING = "SCREENING", "Screening"
        INTERVIEW = "INTERVIEW", "Interview"
        OFFERED = "OFFERED", "Offered"
        HIRED = "HIRED", "Hired"
        REJECTED = "REJECTED", "Rejected"
        WITHDRAWN = "WITHDRAWN", "Withdrawn"

    class Source(models.TextChoices):
        WEBSITE = "WEBSITE", "Website form"
        IMPORT = "IMPORT", "Legacy import"

    # The legacy form's choice lists (kept verbatim: they are what the website submits).
    class Experience(models.TextChoices):
        UP_TO_1 = "0–1 years", "0–1 years"
        ONE_TO_3 = "1–3 years", "1–3 years"
        THREE_TO_5 = "3–5 years", "3–5 years"
        FIVE_PLUS = "5+ years", "5+ years"

    class Salary(models.TextChoices):
        BELOW_3 = "Below ₹3 LPA", "Below ₹3 LPA"
        FROM_3_TO_5 = "₹3–5 LPA", "₹3–5 LPA"
        FROM_5_TO_8 = "₹5–8 LPA", "₹5–8 LPA"
        FROM_8_TO_12 = "₹8–12 LPA", "₹8–12 LPA"
        ABOVE_12 = "₹12+ LPA", "₹12+ LPA"

    class NoticePeriod(models.TextChoices):
        IMMEDIATE = "Immediate", "Immediate"
        DAYS_15 = "15 days", "15 days"
        MONTH_1 = "1 month", "1 month"
        MONTHS_2 = "2 months", "2 months"
        MONTHS_3 = "3 months", "3 months"

    class HeardAbout(models.TextChoices):
        LINKEDIN = "LinkedIn", "LinkedIn"
        JOB_PORTAL = "Job Portal", "Job Portal"
        REFERRAL = "Referral", "Referral"
        COMPANY_WEBSITE = "Company Website", "Company Website"
        SOCIAL_MEDIA = "Social Media", "Social Media"
        OTHER = "Other", "Other"

    # Which posting (null for a general application). PROTECT: postings are archived, never deleted under a candidate.
    position = models.ForeignKey(JobPosition, null=True, blank=True, on_delete=models.PROTECT, related_name="applications")
    position_label = models.CharField(max_length=200, default=GENERAL_APPLICATION, help_text="Legacy free-text `position`.")
    position_title = models.CharField(max_length=200, blank=True, default="", help_text="Posting title snapshot at submission/assignment.")
    department_name = models.CharField(max_length=120, blank=True, default="", help_text="Department snapshot (or area of interest).")

    status = models.CharField(max_length=12, choices=Status.choices, default=Status.NEW)
    status_changed_at = models.DateTimeField(null=True, blank=True)
    # Attribution-style: removing a user never removes applications.
    assignee = models.ForeignKey(settings.AUTH_USER_MODEL, null=True, blank=True, on_delete=models.SET_NULL, related_name="+")
    source = models.CharField(max_length=12, choices=Source.choices, default=Source.WEBSITE)
    ip = models.GenericIPAddressField(null=True, blank=True, help_text="Trusted client IP of the submission.")

    name = models.CharField(max_length=255, help_text="Legacy `full_name`.")
    email = models.EmailField(max_length=254)
    phone_e164 = models.CharField(max_length=16)
    location = models.CharField(max_length=255)
    linkedin = models.URLField(max_length=300)
    portfolio_website = models.URLField(max_length=300, blank=True, default="")
    current_company = models.CharField(max_length=255, blank=True, default="")
    current_role = models.CharField(max_length=255, blank=True, default="")
    total_experience = models.CharField(max_length=32, choices=Experience.choices, blank=True, default="")
    relevant_experience = models.CharField(max_length=32, choices=Experience.choices, blank=True, default="")
    current_salary = models.CharField(max_length=32, choices=Salary.choices, blank=True, default="")
    expected_salary = models.CharField(max_length=32, choices=Salary.choices, blank=True, default="")
    notice_period = models.CharField(max_length=32, choices=NoticePeriod.choices, blank=True, default="")
    heard_about_us = models.CharField(max_length=32, choices=HeardAbout.choices, blank=True, default="")
    availability = models.CharField(max_length=32, blank=True, default="")
    cover_letter = models.TextField(blank=True, default="", help_text="Legacy `cover_note` ('Why Flarize?').")
    # Private RESUME assets (media usage guard registered in apps.py). SET_NULL: the row outlives a purged file.
    resume = models.ForeignKey("media.MediaAsset", null=True, blank=True, on_delete=models.SET_NULL, related_name="+")
    # Private RESUME asset as well; SET_NULL for the same reason.
    portfolio = models.ForeignKey("media.MediaAsset", null=True, blank=True, on_delete=models.SET_NULL, related_name="+")
    declaration_accepted = models.BooleanField(default=False)

    class Meta:
        db_table = "careers_job_application"
        ordering = ["-created_at", "-id"]
        indexes = [
            # Staff queue: status filter, newest first.
            models.Index(fields=["status", "created_at"], name="careers_app_status_created"),
            # Applications of one posting (position filter, overview counts).
            models.Index(fields=["position", "status"], name="careers_app_position_status"),
            # Duplicate/candidate lookups from the queue search.
            models.Index(fields=["email"], name="careers_app_email"),
            models.Index(fields=["phone_e164"], name="careers_app_phone"),
        ]
        constraints = [
            models.CheckConstraint(
                condition=Q(status__in=["NEW", "SCREENING", "INTERVIEW", "OFFERED", "HIRED", "REJECTED", "WITHDRAWN"]),
                name="careers_job_application_status_valid",
            ),
            models.CheckConstraint(condition=Q(source__in=["WEBSITE", "IMPORT"]), name="careers_job_application_source_valid"),
            models.CheckConstraint(condition=Q(phone_e164__regex=r"^\+[1-9][0-9]{6,14}$"), name="careers_job_application_phone_e164"),
            models.CheckConstraint(condition=Q(total_experience__in=["", "0–1 years", "1–3 years", "3–5 years", "5+ years"]), name="careers_job_application_total_experience_valid"),
            models.CheckConstraint(condition=Q(relevant_experience__in=["", "0–1 years", "1–3 years", "3–5 years", "5+ years"]), name="careers_job_application_relevant_experience_valid"),
            models.CheckConstraint(condition=Q(current_salary__in=["", "Below ₹3 LPA", "₹3–5 LPA", "₹5–8 LPA", "₹8–12 LPA", "₹12+ LPA"]), name="careers_job_application_current_salary_valid"),
            models.CheckConstraint(condition=Q(expected_salary__in=["", "Below ₹3 LPA", "₹3–5 LPA", "₹5–8 LPA", "₹8–12 LPA", "₹12+ LPA"]), name="careers_job_application_expected_salary_valid"),
            models.CheckConstraint(condition=Q(notice_period__in=["", "Immediate", "15 days", "1 month", "2 months", "3 months"]), name="careers_job_application_notice_period_valid"),
            models.CheckConstraint(
                condition=Q(heard_about_us__in=["", "LinkedIn", "Job Portal", "Referral", "Company Website", "Social Media", "Other"]),
                name="careers_job_application_heard_about_us_valid",
            ),
        ]

    #: Allowed workflow moves. Anything else is refused (409 ``invalid_status_transition``); reopening is deliberate.
    TRANSITIONS = {
        Status.NEW: (Status.SCREENING, Status.REJECTED, Status.WITHDRAWN),
        Status.SCREENING: (Status.INTERVIEW, Status.REJECTED, Status.WITHDRAWN, Status.NEW),
        Status.INTERVIEW: (Status.OFFERED, Status.REJECTED, Status.WITHDRAWN, Status.SCREENING),
        Status.OFFERED: (Status.HIRED, Status.REJECTED, Status.WITHDRAWN, Status.INTERVIEW),
        Status.HIRED: (Status.WITHDRAWN,),
        Status.REJECTED: (Status.SCREENING,),
        Status.WITHDRAWN: (Status.SCREENING,),
    }

    def __str__(self) -> str:
        return f"{self.name} — {self.display_position}"

    @property
    def display_position(self) -> str:
        """The posting title if this answered one, else the free-text position."""
        return self.position_title or self.position_label

    @property
    def is_archived(self) -> bool:
        return self.deleted_at is not None

    def allowed_transitions(self) -> list[str]:
        return [str(status) for status in self.TRANSITIONS.get(self.status, ())]


class JobApplicationNote(BaseModel):
    """An internal note from the hiring team. Never shown to candidates."""

    # True child: notes live and die with their application.
    application = models.ForeignKey(JobApplication, on_delete=models.CASCADE, related_name="notes")
    # Legacy notes carried a Studio username (the CMS users lived in another database); new notes use created_by.
    author_name = models.CharField(max_length=150, blank=True, default="")
    body = models.TextField()

    class Meta:
        db_table = "careers_job_application_note"
        ordering = ["-created_at", "-id"]
        indexes = [models.Index(fields=["application", "created_at"], name="careers_app_note_app_created")]

    def __str__(self) -> str:
        return f"note on {self.application_id}"


class JobApplicationEvent(models.Model):
    """One line of an application's timeline *(no base; append-only)*: received, status, assigned, archived, …"""

    class Kind(models.TextChoices):
        RECEIVED = "RECEIVED", "Application received"
        STATUS = "STATUS", "Status changed"
        ASSIGNED = "ASSIGNED", "Linked to a position"
        ASSIGNEE = "ASSIGNEE", "Assignee changed"
        ARCHIVED = "ARCHIVED", "Archived"
        RESTORED = "RESTORED", "Restored"
        NOTE = "NOTE", "Note added"

    id = models.BigAutoField(primary_key=True)
    # True child: the timeline belongs to its application.
    application = models.ForeignKey(JobApplication, on_delete=models.CASCADE, related_name="events")
    kind = models.CharField(max_length=12, choices=Kind.choices)
    from_status = models.CharField(max_length=12, blank=True, default="")
    to_status = models.CharField(max_length=12, blank=True, default="")
    detail = models.CharField(max_length=255, blank=True, default="")
    # Attribution: SET_NULL; ``actor_name`` keeps the display name (and the legacy Studio username on imports).
    actor = models.ForeignKey(settings.AUTH_USER_MODEL, null=True, blank=True, on_delete=models.SET_NULL, related_name="+")
    actor_name = models.CharField(max_length=150, blank=True, default="")
    created_at = models.DateTimeField(default=timezone.now)

    class Meta:
        db_table = "careers_job_application_event"
        ordering = ["-created_at", "-id"]
        indexes = [models.Index(fields=["application", "created_at"], name="careers_app_event_app_created")]
        constraints = [
            models.CheckConstraint(
                condition=Q(kind__in=["RECEIVED", "STATUS", "ASSIGNED", "ASSIGNEE", "ARCHIVED", "RESTORED", "NOTE"]),
                name="careers_job_application_event_kind_valid",
            ),
        ]

    def __str__(self) -> str:
        return f"{self.kind} on {self.application_id}"
