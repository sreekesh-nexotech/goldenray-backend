"""``careers/positions/`` — job postings, their editor and the publish workflow.

Workflow (POST sub-resources, each accepting ``expected_version``):

========== ============================== ============== ======================================================
action     from                           to             notes
========== ============================== ============== ======================================================
publish    DRAFT, CLOSED, ARCHIVED        PUBLISHED      refused while :func:`publish_errors` lists problems
                                                         (400 ``position_not_ready``); keeps the first
                                                         ``published_at``; clears ``closed_at``
unpublish  PUBLISHED, CLOSED              DRAFT          off the website; applications untouched
close      PUBLISHED                      CLOSED         stops applications, the page stays readable
archive    DRAFT, PUBLISHED, CLOSED       ARCHIVED       hidden everywhere public; history kept
========== ============================== ============== ======================================================

Repeating an action on a row already in the target state is a no-op (200, version unchanged); any other move is
409 ``invalid_status_transition``. ``DELETE`` soft-deletes a posting nobody applied to (409
``position_has_applications`` otherwise — archive it). Every write is audited, bumps ``careers:positions`` and
emits ``careers.position_<action>`` for website revalidation.
"""

from __future__ import annotations

from django.db import IntegrityError, transaction
from django.db.models import Count, Q
from django.utils import timezone

from audit.services import changes, record, snapshot
from careers.models import Department, JobApplication, JobPosition
from careers.services.slugs import derive_slug
from core.errors import Conflict, DomainError
from core.outbox import emit
from core.services import check_version, stamp_create
from flarize.cache_utils import bump

CACHE_NAMESPACE = "careers:positions"
Status = JobPosition.Status

SEO_FIELDS = ("seo_title", "meta_description", "canonical_url", "og_title", "og_description", "og_image", "schema_type", "schema_extra", "noindex")
CONTENT_FIELDS = (
    "title",
    "slug",
    "department",
    "location",
    "employment_type",
    "experience_required",
    "description",
    "responsibilities",
    "requirements",
    "benefits",
    "application_instructions",
    "opens_on",
    "application_deadline",
    "openings",
    "sort_order",
)
EDITABLE_FIELDS = CONTENT_FIELDS + SEO_FIELDS
SNAPSHOT_FIELDS = EDITABLE_FIELDS + ("status", "published_at", "closed_at")

PAST = {"publish": "published", "unpublish": "unpublished", "close": "closed", "archive": "archived"}
TRANSITIONS = {
    "publish": ((Status.DRAFT, Status.CLOSED, Status.ARCHIVED), Status.PUBLISHED),
    "unpublish": ((Status.PUBLISHED, Status.CLOSED), Status.DRAFT),
    "close": ((Status.PUBLISHED,), Status.CLOSED),
    "archive": ((Status.DRAFT, Status.PUBLISHED, Status.CLOSED), Status.ARCHIVED),
}


def positions_queryset():
    """Staff list/detail rows with department, OG image and the live application count (no N+1)."""
    return (
        JobPosition.objects.select_related("department", "og_image")
        .annotate(application_count=Count("applications", filter=Q(applications__deleted_at__isnull=True), distinct=True))
        .order_by("sort_order", "-published_at", "-created_at", "id")
    )


def position_snapshot(position: JobPosition) -> dict:
    return snapshot(position, SNAPSHOT_FIELDS)


def publish_errors(position: JobPosition) -> list[str]:
    """What stops this posting from going live (legacy §6.12 publish gate)."""
    errors = []
    if not (position.title or "").strip():
        errors.append("A job title is required.")
    if not position.department_id:
        errors.append("Choose a department.")
    elif not position.department.is_active or position.department.deleted_at is not None:
        errors.append(f"'{position.department.name}' is inactive — reactivate it or pick another department.")
    if not (position.location or "").strip():
        errors.append("A location is required.")
    if not (position.description or "").strip():
        errors.append("A job description is required.")
    return errors


def _slug_taken() -> Conflict:
    return Conflict("position_slug_taken", "Another live position already uses this slug.", errors={"slug": ["Already in use."]})


def _validate(values: dict) -> None:
    department = values.get("department")
    if department is not None and department.deleted_at is not None:
        raise DomainError("validation_error", "Unknown department.", errors={"department": ["This department no longer exists."]})
    opens_on, deadline = values.get("opens_on"), values.get("application_deadline")
    if opens_on and deadline and deadline < opens_on:
        raise DomainError("validation_error", "The deadline is before the opening date.", errors={"application_deadline": ["Must be on or after opens_on."]})


def _derive_slug(title: str) -> str:
    slug = derive_slug(title, 220)
    if not slug:
        raise DomainError("validation_error", "A slug is required.", errors={"slug": ["Could not derive a slug from the title; enter one."]})
    return slug


@transaction.atomic
def create_position(*, user, data) -> JobPosition:
    values = {name: data[name] for name in EDITABLE_FIELDS if name in data}
    values["slug"] = (values.get("slug") or "").strip() or _derive_slug(values.get("title", ""))
    _validate(values)
    position = JobPosition(**values, status=Status.DRAFT)
    stamp_create(position, user)
    try:
        with transaction.atomic():
            position.save()
    except IntegrityError:
        raise _slug_taken() from None
    record("careers.position_created", obj=position, actor=user, after=position_snapshot(position))
    bump(CACHE_NAMESPACE)
    return position


@transaction.atomic
def update_position(instance: JobPosition, *, user, data, expected_version=None) -> JobPosition:
    position = JobPosition.objects.select_for_update().select_related("department").get(pk=instance.pk)
    check_version(position, expected_version)
    before = position_snapshot(position)
    values = {name: data[name] for name in EDITABLE_FIELDS if name in data and data[name] != getattr(position, name)}
    if "slug" in values and not (values["slug"] or "").strip():
        values["slug"] = _derive_slug(values.get("title", position.title))
    if not values:
        return position
    _validate({"opens_on": position.opens_on, "application_deadline": position.application_deadline, **values})
    try:
        with transaction.atomic():
            position.versioned_update(user, **values)
    except IntegrityError:
        raise _slug_taken() from None
    changed_before, changed_after = changes(before, position_snapshot(position))
    record("careers.position_updated", obj=position, actor=user, before=changed_before, after=changed_after)
    bump(CACHE_NAMESPACE)
    if position.status in (Status.PUBLISHED, Status.CLOSED):
        # A new slug moves the page: the old URL must be revalidated too (it answers 404 now).
        _emit(position, "careers.position_updated", previous_path=f"/career/{before['slug']}" if "slug" in values else None)
    return position


def _emit(position: JobPosition, event_type: str, *, previous_path: str | None = None) -> None:
    payload = {"position_uid": str(position.uid), "slug": position.slug, "status": position.status, "path": position.seo_path()}
    if previous_path:
        payload["previous_path"] = previous_path
    emit(event_type, payload, aggregate_type="careers.jobposition", aggregate_uid=position.uid)


@transaction.atomic
def transition(instance: JobPosition, action: str, *, user, expected_version=None) -> JobPosition:
    """Apply one workflow ``action`` (``publish``/``unpublish``/``close``/``archive``) — see the module table."""
    allowed_from, target = TRANSITIONS[action]
    position = JobPosition.objects.select_for_update().select_related("department").get(pk=instance.pk)
    check_version(position, expected_version)
    if position.status == target:
        return position
    if position.status not in allowed_from:
        raise Conflict(
            "invalid_status_transition",
            f"Cannot {action} a position that is {position.status}.",
            errors={"status": [f"Allowed from: {', '.join(allowed_from)}."]},
        )
    now = timezone.now()
    values = {"status": target}
    if action == "publish":
        problems = publish_errors(position)
        if problems:
            raise DomainError("position_not_ready", "This position is not ready to publish.", errors={"publish_errors": problems})
        values.update(published_at=position.published_at or now, closed_at=None)
    elif action == "close":
        values["closed_at"] = now
    before = {"status": position.status}
    position.versioned_update(user, **values)
    record(f"careers.position_{PAST[action]}", obj=position, actor=user, before=before, after={"status": position.status})
    bump(CACHE_NAMESPACE)
    _emit(position, f"careers.position_{PAST[action]}")
    return position


@transaction.atomic
def delete_position(instance: JobPosition, *, user, expected_version=None) -> None:
    position = JobPosition.objects.select_for_update().get(pk=instance.pk)
    check_version(position, expected_version)
    if JobApplication.all_objects.filter(position=position).exists():
        raise Conflict("position_has_applications", "Candidates applied to this position; archive it instead of deleting it.")
    was_public = position.status in (Status.PUBLISHED, Status.CLOSED)
    position.soft_delete(user)
    record("careers.position_deleted", obj=position, actor=user, before=position_snapshot(position))
    bump(CACHE_NAMESPACE)
    if was_public:
        _emit(position, "careers.position_deleted")


def overview(*, include_applications: bool) -> dict:
    """Careers dashboard counts (legacy ``careers/overview/``) plus up to ten open positions."""
    positions = JobPosition.objects.exclude(status=Status.ARCHIVED)
    counts = positions.aggregate(
        active_positions=Count("id", filter=Q(status=Status.PUBLISHED)),
        draft_positions=Count("id", filter=Q(status=Status.DRAFT)),
        closed_positions=Count("id", filter=Q(status=Status.CLOSED)),
    )
    counts["departments"] = Department.objects.filter(is_active=True).count()
    if include_applications:
        applications = JobApplication.objects.aggregate(
            applications_total=Count("id"),
            applications_new=Count("id", filter=Q(status=JobApplication.Status.NEW)),
        )
        counts.update(applications)
    open_positions = list(positions_queryset().filter(status=Status.PUBLISHED)[:10])
    return {"counts": counts, "open_positions": open_positions}
