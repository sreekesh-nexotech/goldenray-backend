"""Website careers payloads: ``GET job-positions/`` and ``GET job-positions/<slug>/`` (public, cached).

The payloads are the legacy CMS delivery contract (``cms/careers/public.py``) field for field, with one change: the
posting's integer ``id`` is its ``uid`` (integer ids never leave the service layer; DV-47). Parity with the legacy
CMS is asserted by ``careers/tests/test_public_parity.py`` against recorded goldens.

* the list serves ``PUBLISHED`` postings only (``?department=<slug>`` narrows it), ordered like the CMS
  (``sort_order``, newest ``published_at``, newest ``created_at``), at most :data:`PUBLIC_LIST_LIMIT`;
* ``meta.departments`` lists active departments with at least one published posting; ``meta.intro`` and
  ``meta.accepting_general_applications`` come from the company profile (the CMS ``SiteSettings`` fields);
* the detail answers for ``PUBLISHED`` and ``CLOSED`` (``is_open: false``); drafts, archived and unknown slugs
  are 404. ``meta.schema`` is the JobPosting JSON-LD built from the record (``seo.schema.job_posting``) with the
  company's website as the site URL and its trade name as the hiring organisation.
"""

from __future__ import annotations

from careers.models import Department, JobPosition
from careers.services.positions import publish_errors
from company.services.profile import current_profile
from core.errors import NotFound
from seo import schema as schema_builders

CACHE_NAMESPACES = ("careers:positions", "company")
PUBLIC_LIST_LIMIT = 200  # a hard bound on an otherwise unpaginated legacy contract; the site has a handful of postings
Status = JobPosition.Status


def lines(value: str) -> list[str]:
    """Split a one-per-line textarea into a list, dropping blanks."""
    return [line.strip() for line in (value or "").splitlines() if line.strip()]


def _iso(value) -> str | None:
    return value.isoformat() if value else None


def card(position: JobPosition) -> dict:
    return {
        "uid": str(position.uid),
        "slug": position.slug,
        "title": position.title,
        "department": position.department.name if position.department_id else None,
        "location": position.location,
        "employment_type": position.get_employment_type_display(),
        "experience_required": position.experience_required,
        "application_deadline": _iso(position.application_deadline),
        "published_at": _iso(position.published_at),
        "is_open": position.is_open,
    }


def _site() -> tuple[str, str, object]:
    profile = current_profile()
    return (profile.website or ""), profile.display_name, profile


def public_list(*, department: str | None = None) -> dict:
    positions = JobPosition.objects.filter(status=Status.PUBLISHED).select_related("department").order_by("sort_order", "-published_at", "-created_at", "id")
    if department:
        positions = positions.filter(department__slug=department)
    rows = list(positions[:PUBLIC_LIST_LIMIT])
    departments = (
        Department.objects.filter(is_active=True, positions__status=Status.PUBLISHED, positions__deleted_at__isnull=True).distinct().order_by("sort_order", "name", "id").values("name", "slug")
    )
    _, _, profile = _site()
    return {
        "data": [card(position) for position in rows],
        "meta": {
            "count": len(rows),
            "departments": [{"name": row["name"], "slug": row["slug"]} for row in departments],
            "accepting_general_applications": profile.careers_accepting_general_applications,
            "intro": profile.careers_intro,
        },
    }


def job_posting_schema(position: JobPosition, *, site_url: str, organisation: str) -> dict | None:
    return schema_builders.job_posting(position, site_url=site_url, organisation=organisation)


def public_detail(slug: str) -> dict:
    position = JobPosition.objects.filter(slug=slug, status__in=(Status.PUBLISHED, Status.CLOSED)).select_related("department").first()
    if position is None:
        raise NotFound("not_found", "This position does not exist.")
    site_url, organisation, _ = _site()
    return {
        "data": {
            **card(position),
            "description": position.description,
            "responsibilities": lines(position.responsibilities),
            "requirements": lines(position.requirements),
            "benefits": lines(position.benefits),
            "application_instructions": position.application_instructions,
            "seo": {
                "title": position.seo_title or position.title,
                "description": position.meta_description,
                "canonical_url": position.canonical_url,
                "noindex": position.noindex,
            },
        },
        "meta": {"schema": job_posting_schema(position, site_url=site_url, organisation=organisation)},
    }


def preview(position: JobPosition) -> dict:
    """The posting as the public page will render it (staff ``…/preview/``), whatever its status."""
    site_url, organisation, _ = _site()
    return {
        "url": f"{site_url.rstrip('/')}{position.seo_path()}",
        "title": position.seo_title or position.title,
        "description": position.meta_description,
        "department": position.department.name if position.department_id else None,
        "location": position.location,
        "employment_type": position.get_employment_type_display(),
        "schema": job_posting_schema(position, site_url=site_url, organisation=organisation),
        "seo_issues": position.seo_issues(),
        "publish_errors": publish_errors(position),
    }
