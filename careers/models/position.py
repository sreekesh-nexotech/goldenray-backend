"""``careers_job_position`` (PLAN §2.8): one job posting + the shared SEO block (``seo.models.SeoFields``).

Column names follow the legacy CMS (``experience_required``, ``application_deadline``, ``responsibilities``,
``benefits``, ``application_instructions``, ``published_at``, ``closed_at``, ``sort_order``) so the Studio contract,
the JobPosting JSON-LD builder and the importer map field for field; PLAN's ``experience``/``closes_on`` are those
columns (DV-17). ``opens_on`` and ``openings`` are the PLAN's additions.

``status`` is what the website filters on: the public list serves ``PUBLISHED`` only; the public detail also answers
for ``CLOSED`` (``is_open: false``) so a bookmarked posting can say it no longer accepts applications.
"""

from __future__ import annotations

from django.db import models
from django.db.models import F, Q

from careers.models.department import SLUG_REGEX, Department
from core.models import BaseModel
from seo.models import SchemaType, SeoFields


class JobPosition(BaseModel, SeoFields):
    class Status(models.TextChoices):
        DRAFT = "DRAFT", "Draft"
        PUBLISHED = "PUBLISHED", "Published"
        CLOSED = "CLOSED", "Closed"
        ARCHIVED = "ARCHIVED", "Archived"

    class EmploymentType(models.TextChoices):
        # Legacy CMS values, kept verbatim: they are the Studio vocabulary and the JSON-LD ``employmentType``.
        FULL_TIME = "full_time", "Full-time"
        PART_TIME = "part_time", "Part-time"
        CONTRACT = "contract", "Contract"
        INTERNSHIP = "internship", "Internship"
        TEMPORARY = "temporary", "Temporary"

    title = models.CharField(max_length=200)
    slug = models.SlugField(max_length=220, help_text="Public URL: /career/<slug>")
    # Master data: a department with positions cannot be deleted (the service refuses first, 409).
    department = models.ForeignKey(Department, on_delete=models.PROTECT, related_name="positions")
    location = models.CharField(max_length=160)
    employment_type = models.CharField(max_length=16, choices=EmploymentType.choices, default=EmploymentType.FULL_TIME)
    experience_required = models.CharField(max_length=120, blank=True, default="", help_text="e.g. '2–4 years' (PLAN: experience)")
    description = models.TextField(blank=True, default="")
    responsibilities = models.TextField(blank=True, default="", help_text="One per line.")
    requirements = models.TextField(blank=True, default="", help_text="One per line.")
    benefits = models.TextField(blank=True, default="", help_text="One per line.")
    application_instructions = models.TextField(blank=True, default="")
    opens_on = models.DateField(null=True, blank=True, help_text="First day applications are accepted (informational).")
    application_deadline = models.DateField(null=True, blank=True, help_text="Last day to apply (PLAN: closes_on); JSON-LD validThrough.")
    openings = models.PositiveSmallIntegerField(null=True, blank=True, help_text="Number of vacancies, when known.")
    status = models.CharField(max_length=12, choices=Status.choices, default=Status.DRAFT)
    sort_order = models.IntegerField(default=0)
    published_at = models.DateTimeField(null=True, blank=True)
    closed_at = models.DateTimeField(null=True, blank=True)

    class Meta:
        db_table = "careers_job_position"
        ordering = ["sort_order", "-published_at", "-created_at", "id"]
        indexes = [
            # Public list and staff status filter: status, then the display order.
            models.Index(fields=["status", "sort_order"], name="careers_position_status_order"),
            # Staff list filtered by department and status.
            models.Index(fields=["department", "status"], name="careers_position_dept_status"),
        ]
        constraints = [
            models.UniqueConstraint(fields=["slug"], condition=Q(deleted_at__isnull=True), name="careers_job_position_slug_live_uniq"),
            models.CheckConstraint(condition=Q(slug__regex=SLUG_REGEX), name="careers_job_position_slug_format"),
            models.CheckConstraint(condition=Q(status__in=["DRAFT", "PUBLISHED", "CLOSED", "ARCHIVED"]), name="careers_job_position_status_valid"),
            models.CheckConstraint(
                condition=Q(employment_type__in=["full_time", "part_time", "contract", "internship", "temporary"]),
                name="careers_job_position_employment_type_valid",
            ),
            # SeoFields.schema_type is an enum column of this table (the abstract mixin cannot declare the CHECK).
            models.CheckConstraint(condition=Q(schema_type__in=SchemaType.values), name="careers_job_position_schema_type_valid"),
            models.CheckConstraint(condition=Q(openings__isnull=True) | Q(openings__gte=1), name="careers_job_position_openings_positive"),
            models.CheckConstraint(
                condition=Q(opens_on__isnull=True) | Q(application_deadline__isnull=True) | Q(application_deadline__gte=F("opens_on")),
                name="careers_job_position_dates_ordered",
            ),
        ]

    def __str__(self) -> str:
        return f"{self.title} [{self.status}]"

    @property
    def is_open(self) -> bool:
        return self.status == self.Status.PUBLISHED

    # SEO hooks (seo.models.SeoFields)
    def seo_fallback_title(self) -> str:
        return self.title

    def seo_path(self) -> str:
        return f"/career/{self.slug}"
