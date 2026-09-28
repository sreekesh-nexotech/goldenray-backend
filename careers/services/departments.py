"""``careers/departments/`` — hiring departments.

* names (case-insensitive) and slugs are unique among live departments (409 ``department_name_taken`` /
  ``department_slug_taken``);
* a department that still has live positions cannot be deleted (409 ``department_in_use``): deactivate it instead,
  or move those positions first — the legacy Studio rule, now a stable error code instead of a 400 text;
* the list carries ``job_count`` (every live position, archived ones included — that is what blocks deletion) and
  ``open_job_count`` (published) as annotations, so the list is two queries whatever its length.
"""

from __future__ import annotations

from django.db import IntegrityError, transaction
from django.db.models import Count, Q
from django.utils.text import slugify

from audit.services import changes, record, snapshot
from careers.models import Department, JobPosition
from core.errors import Conflict, DomainError
from core.services import check_version, stamp_create
from flarize.cache_utils import bump

CACHE_NAMESPACE = "careers:positions"  # departments are embedded in the public position payloads
SNAPSHOT_FIELDS = ("name", "slug", "description", "is_active", "sort_order")
EDITABLE_FIELDS = SNAPSHOT_FIELDS


def departments_queryset():
    live_positions = Q(positions__deleted_at__isnull=True)
    return Department.objects.annotate(
        job_count=Count("positions", filter=live_positions, distinct=True),
        open_job_count=Count("positions", filter=live_positions & Q(positions__status=JobPosition.Status.PUBLISHED), distinct=True),
    ).order_by("sort_order", "name", "id")


def department_snapshot(department: Department) -> dict:
    return snapshot(department, SNAPSHOT_FIELDS)


def _unique_violation(exc: IntegrityError) -> Conflict:
    text = str(exc)
    if "careers_department_name_live_uniq" in text:
        return Conflict("department_name_taken", "Another department already uses this name.", errors={"name": ["Already in use."]})
    return Conflict("department_slug_taken", "Another department already uses this slug.", errors={"slug": ["Already in use."]})


def _slug_for(data: dict, fallback_name: str) -> str:
    slug = (data.get("slug") or "").strip() or slugify(fallback_name)[:120].strip("-")
    if not slug:
        raise DomainError("validation_error", "A slug is required.", errors={"slug": ["Could not derive a slug from the name; enter one."]})
    return slug


@transaction.atomic
def create_department(*, user, data) -> Department:
    department = Department(
        name=data["name"].strip(),
        slug=_slug_for(data, data["name"]),
        description=data.get("description", ""),
        is_active=data.get("is_active", True),
        sort_order=data.get("sort_order", 0),
    )
    stamp_create(department, user)
    try:
        with transaction.atomic():
            department.save()
    except IntegrityError as exc:
        raise _unique_violation(exc) from None
    record("careers.department_created", obj=department, actor=user, after=department_snapshot(department))
    bump(CACHE_NAMESPACE)
    return department


@transaction.atomic
def update_department(instance: Department, *, user, data, expected_version=None) -> Department:
    department = Department.objects.select_for_update().get(pk=instance.pk)
    check_version(department, expected_version)
    before = department_snapshot(department)
    values = {}
    for name in EDITABLE_FIELDS:
        if name not in data:
            continue
        value = data[name].strip() if name in ("name", "slug") else data[name]
        if name == "slug" and not value:
            value = _slug_for({}, data.get("name", department.name))
        if value != getattr(department, name):
            values[name] = value
    if not values:
        return department
    try:
        with transaction.atomic():
            department.versioned_update(user, **values)
    except IntegrityError as exc:
        raise _unique_violation(exc) from None
    changed_before, changed_after = changes(before, department_snapshot(department))
    record("careers.department_updated", obj=department, actor=user, before=changed_before, after=changed_after)
    bump(CACHE_NAMESPACE)
    return department


@transaction.atomic
def delete_department(instance: Department, *, user, expected_version=None) -> None:
    department = Department.objects.select_for_update().get(pk=instance.pk)
    check_version(department, expected_version)
    dependants = JobPosition.objects.filter(department=department).count()
    if dependants:
        raise Conflict(
            "department_in_use",
            f"{dependants} job position(s) belong to '{department.name}'. Deactivate the department instead, or move those positions first.",
            errors={"positions": [str(dependants)]},
        )
    department.soft_delete(user)
    record("careers.department_deleted", obj=department, actor=user, before=department_snapshot(department))
    bump(CACHE_NAMESPACE)
