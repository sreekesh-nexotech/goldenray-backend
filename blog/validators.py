"""Value validators for blog records (kept out of ``services`` so model fields and migrations can reference them).

A slug is a permanent public URL, so the legacy CMS rules are kept exactly (CMS_BLUEPRINT §6.1): **malformed**
(not ``slugify()`` shape), **placeholder** (``test``, ``asdf-2``, ``objectobject`` …, matched against four forms of
the value) and **reserved** (paths the site owns). The checks run in the order empty → whitespace → max length →
regex → min length → reserved → placeholder, so ``AB`` reports "malformed" and ``ab`` "too short".

Field validators never run on a bare ``.save()`` (the legacy weakness §17 #10), so the services call
:func:`validate_entry_slug` explicitly on every write path; it is also attached to the model field for
``full_clean()`` callers.
"""

from __future__ import annotations

import re

from django.core.exceptions import ValidationError

SLUG_RE = re.compile(r"^[a-z0-9]+(?:-[a-z0-9]+)*$")
MIN_SLUG_LENGTH = 3
MAX_SLUG_LENGTH = 255

PLACEHOLDER_SLUGS = frozenset(
    {
        "sdf",
        "sdfsdf",
        "test",
        "tests",
        "testing",
        "testtest",
        "asdf",
        "asdfasdf",
        "qwerty",
        "qwertyuiop",
        "undefined",
        "null",
        "none",
        "nan",
        "objectobject",
        "slug",
        "string",
        "foo",
        "bar",
        "foobar",
        "baz",
        "abc",
        "xyz",
        "aaa",
        "xxx",
        "temp",
        "tmp",
        "dummy",
        "sample",
        "example",
        "placeholder",
        "lorem",
        "loremipsum",
        "new",
        "untitled",
        "draft",
        "demo",
        "delete",
        "deleteme",
        "dontuse",
        "changeme",
    }
)
RESERVED_SLUGS = frozenset({"admin", "api", "blog", "static", "media", "uploads", "feed", "rss", "sitemap", "robots", "search", "page", "index"})
_TRAILING_COUNTER_RE = re.compile(r"-\d+$")

# Collection ``api_uid`` is a public route segment (``content/<api_uid>/``); ``preview`` is taken by the preview route.
API_UID_RE = re.compile(r"^[a-z0-9]+(?:-[a-z0-9]+)*$")
RESERVED_API_UIDS = frozenset({"preview"})
# Template image-group / attribute-slot keys are the delivery contract (``imgUrls.<key>``, ``attributes.<key>``).
KEY_RE = re.compile(r"^[A-Za-z][A-Za-z0-9_]{0,59}$")
# Site-relative path prefix a collection is published under (``/blog``); used for sitemap and revalidation paths.
PATH_PREFIX_RE = re.compile(r"^(/[a-z0-9]+(?:-[a-z0-9]+)*)+$")
COLOR_RE = re.compile(r"^#(?:[0-9A-Fa-f]{3}|[0-9A-Fa-f]{6}|[0-9A-Fa-f]{8})$")
# Strapi component uid (``category.name``) emitted as ``contentBlocks[].__component``.
COMPONENT_RE = re.compile(r"^[a-z0-9]+(?:-[a-z0-9]+)*\.[a-z0-9]+(?:-[a-z0-9]+)*$")


def _placeholder_forms(value: str) -> set[str]:
    stripped = _TRAILING_COUNTER_RE.sub("", value)
    return {value, value.replace("-", ""), stripped, stripped.replace("-", "")}


def validate_entry_slug(value) -> None:
    """Raise ``ValidationError`` unless ``value`` is a usable public slug (uniqueness is the service's job)."""
    if value is None:
        raise ValidationError("A slug is required.", code="slug_empty")
    raw = str(value)
    if raw != raw.strip():
        raise ValidationError("Slug must not start or end with whitespace.", code="slug_whitespace")
    if not raw:
        raise ValidationError("A slug is required.", code="slug_empty")
    if len(raw) > MAX_SLUG_LENGTH:
        raise ValidationError(f"Slug must be at most {MAX_SLUG_LENGTH} characters (got {len(raw)}).", code="slug_too_long")
    if not SLUG_RE.match(raw):
        raise ValidationError(
            "Slug must be lowercase letters, numbers and single hyphens only (e.g. 'solar-panel-cost-in-kerala'). Got %(value)s.",
            code="slug_malformed",
            params={"value": repr(raw)},
        )
    if len(raw) < MIN_SLUG_LENGTH:
        raise ValidationError(f"Slug must be at least {MIN_SLUG_LENGTH} characters.", code="slug_too_short")
    if raw in RESERVED_SLUGS:
        raise ValidationError("'%(value)s' is a reserved site path and cannot be used as a slug.", code="slug_reserved", params={"value": raw})
    if _placeholder_forms(raw) & PLACEHOLDER_SLUGS:
        raise ValidationError(
            "'%(value)s' looks like placeholder or test content. Give the entry its real URL slug before saving.",
            code="slug_placeholder",
            params={"value": raw},
        )


def slug_error(value) -> str | None:
    """:func:`validate_entry_slug` as a nullable message (availability endpoint, import reports)."""
    try:
        validate_entry_slug(value)
    except ValidationError as exc:
        return exc.messages[0]
    return None


def slug_error_code(value) -> str | None:
    try:
        validate_entry_slug(value)
    except ValidationError as exc:
        return exc.error_list[0].code
    return None


def validate_api_uid(value) -> None:
    raw = str(value or "")
    if not API_UID_RE.match(raw) or len(raw) > 80:
        raise ValidationError("Use lowercase letters, numbers and single hyphens (e.g. 'case-studies'), at most 80 characters.", code="api_uid_malformed")
    if raw in RESERVED_API_UIDS:
        raise ValidationError(f"'{raw}' is reserved by the delivery API.", code="api_uid_reserved")


def validate_key(value) -> None:
    if not KEY_RE.match(str(value or "")):
        raise ValidationError("Start with a letter; use letters, digits and underscores only (e.g. 'coverImg'), at most 60 characters.", code="key_malformed")


def validate_path_prefix(value) -> None:
    raw = str(value or "")
    if len(raw) > 120 or not PATH_PREFIX_RE.match(raw):
        raise ValidationError("A site-relative path such as '/blog' (lowercase, no trailing slash).", code="path_prefix_malformed")


def validate_color(value) -> None:
    if not COLOR_RE.match(str(value or "")):
        raise ValidationError("A hex colour such as '#ED8723'.", code="color_malformed")


def validate_component(value) -> None:
    raw = str(value or "")
    if len(raw) > 120 or not COMPONENT_RE.match(raw):
        raise ValidationError("A component uid such as 'shared.rich-text'.", code="component_malformed")
