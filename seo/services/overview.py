"""The cross-module SEO overview (staff ``seo/overview/``): every SEO-bearing record with its validity indicator.

The legacy screen evaluated issues in Python over full table scans and returned everything (CMS_BLUEPRINT §12.4,
§17 #15). Here each provider returns a queryset annotated with ``seo_status`` **in SQL** (the rules of
:func:`seo.models.seo_issues_for`, expressed by :func:`seo_status_case`), the providers are combined with
``UNION ALL``, filtered, ordered worst-first and paginated by the database; the per-row ``issues`` messages are
computed only for the rows of the requested page, and ``counts`` are grouped in SQL.

Provider contract (``<app>/services/seo_overview.py``): ``SEO_OVERVIEW_KIND = "<kind>"`` and
``seo_overview_rows()`` returning :func:`overview_rows` over the app's records.
"""

from __future__ import annotations

from django.db.models import BooleanField, Case, CharField, Count, F, Func, IntegerField, Q, QuerySet, Value, When
from django.db.models.functions import Coalesce, Length

from seo.models import DESCRIPTION_MAX, DESCRIPTION_MIN, TITLE_MAX, seo_issues_for
from seo.services.providers import overview_providers

STATUSES = ("error", "warning", "ok")
# Column order is part of the UNION contract: every provider builds its rows with overview_rows().
COLUMNS = (
    "o_severity",
    "o_kind",
    "o_uid",
    "o_label",
    "o_path",
    "o_record_status",
    "o_seo_title",
    "o_meta_description",
    "o_effective_title",
    "o_schema_type",
    "o_noindex",
    "o_seo_status",
    "o_updated_at",
)


class StripWhitespace(Func):
    """SQL counterpart of Python's ``str.strip()`` used by :func:`seo.models.seo_issues_for`.

    ``TRIM()`` removes spaces only; pasted titles and descriptions often carry newlines and tabs, which Python strips,
    so the lengths — and the status at the 60/70/160 boundaries — would disagree. POSIX ``[[:space:]]`` covers space,
    tab, newline, carriage return, vertical tab and form feed.
    """

    function = "REGEXP_REPLACE"
    template = "%(function)s(%(expressions)s, '^[[:space:]]+|[[:space:]]+$', '', 'g')"
    output_field = CharField()


def seo_status_case() -> Case:
    """``error`` | ``warning`` | ``ok`` from the annotated title/description lengths (mirrors ``seo_issues_for``)."""
    error = Q(o_title_length=0) | (Q(o_description_length=0) & Q(o_noindex=False))
    warning = Q(o_title_length__gt=TITLE_MAX) | Q(o_description_length=0) | Q(o_description_length__gt=DESCRIPTION_MAX) | Q(o_description_length__lt=DESCRIPTION_MIN)
    return Case(When(error, then=Value("error")), When(warning, then=Value("warning")), default=Value("ok"), output_field=CharField())


def overview_rows(queryset: QuerySet, *, kind: str, label, path, record_status, seo_title, meta_description, fallback_title, schema_type, noindex, uid=None, updated_at=None) -> QuerySet:
    """Annotate ``queryset`` with the overview columns and return ``values()`` in :data:`COLUMNS` order.

    ``seo_title``/``meta_description``/``schema_type``/``noindex`` may be NULL (a record without an SEO block); the
    effective title is the SEO title when set, else ``fallback_title`` — as ``SeoFields.seo_issues`` does.
    """
    annotated = queryset.annotate(
        o_kind=Value(kind, output_field=CharField()),
        o_uid=uid if uid is not None else F("uid"),
        o_label=label,
        o_path=path,
        o_record_status=record_status,
        o_seo_title=Coalesce(seo_title, Value(""), output_field=CharField()),
        o_meta_description=Coalesce(meta_description, Value(""), output_field=CharField()),
        o_schema_type=Coalesce(schema_type, Value("none"), output_field=CharField()),
        o_noindex=Coalesce(noindex, Value(False), output_field=BooleanField()),
        o_updated_at=updated_at if updated_at is not None else F("updated_at"),
    )
    annotated = annotated.annotate(
        o_effective_title=Case(When(~Q(o_seo_title=""), then=F("o_seo_title")), default=Coalesce(fallback_title, Value(""), output_field=CharField()), output_field=CharField()),
    )
    annotated = annotated.annotate(o_title_length=Length(StripWhitespace("o_effective_title")), o_description_length=Length(StripWhitespace("o_meta_description")))
    annotated = annotated.annotate(o_seo_status=seo_status_case())
    annotated = annotated.annotate(o_severity=Case(When(o_seo_status="error", then=Value(0)), When(o_seo_status="warning", then=Value(1)), default=Value(2), output_field=IntegerField()))
    return annotated.values(*COLUMNS)


def _providers(kind: str | None):
    return [provider for provider in overview_providers() if not kind or getattr(provider, "SEO_OVERVIEW_KIND", None) == kind]


def combined(*, kind: str | None = None, status: str | None = None) -> QuerySet | None:
    """Every provider's rows (``UNION ALL``), optionally filtered, worst first; ``None`` when nothing contributes."""
    parts = []
    for provider in _providers(kind):
        rows = provider.seo_overview_rows()
        parts.append(rows.filter(o_seo_status=status) if status else rows)
    if not parts:
        return None
    union = parts[0] if len(parts) == 1 else parts[0].union(*parts[1:], all=True)
    return union.order_by("o_severity", "o_kind", "o_label", "o_uid")


def counts(*, kind: str | None = None) -> dict[str, int]:
    totals = dict.fromkeys(STATUSES, 0)
    for provider in _providers(kind):
        for row in provider.seo_overview_rows().order_by().values("o_seo_status").annotate(n=Count("o_uid")):
            totals[row["o_seo_status"]] += row["n"]
    return totals


def kinds() -> list[str]:
    return sorted({getattr(provider, "SEO_OVERVIEW_KIND", "") for provider in overview_providers()} - {""})


def present(row: dict) -> dict:
    """An overview row for the API (``issues`` computed for this row only)."""
    data = {key[2:]: value for key, value in row.items() if key.startswith("o_")}
    data.pop("severity", None)
    title = data.pop("effective_title")
    data["issues"] = seo_issues_for(title, data["meta_description"], data["noindex"])
    return data
