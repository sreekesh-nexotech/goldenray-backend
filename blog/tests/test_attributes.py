"""Attribute slot options and typed values (strict for the API, lenient for the legacy importer)."""

import pytest

from blog.models import TemplateAttributeSlot
from blog.services.attributes import coerce_value, validate_slot_options
from core.errors import DomainError

Type = TemplateAttributeSlot.Type


def slot(slot_type, **options):
    return TemplateAttributeSlot(key="k", label="K", type=slot_type, options=options)


@pytest.mark.parametrize(
    ("slot_type", "options", "expected"),
    [
        (Type.ENUM, {"choices": ["a", "b"]}, {"choices": ["a", "b"]}),
        (Type.NUMBER, {"min": 0, "max": 10, "default": 5}, {"min": 0, "max": 10, "default": 5}),
        (Type.NUMBER, {}, {}),
        (Type.TEXT, None, {}),
        (Type.URL, {}, {}),
    ],
)
def test_valid_options(slot_type, options, expected):
    assert validate_slot_options(slot_type, options) == expected


@pytest.mark.parametrize(
    ("slot_type", "options"),
    [
        (Type.ENUM, {"choices": ["a"], "extra": 1}),
        (Type.ENUM, {"choices": [" "]}),
        (Type.NUMBER, {"min": "0"}),
        (Type.NUMBER, {"max": 1, "default": 2}),
        (Type.BOOL, {"default": True}),
        (Type.DATE, "x"),
    ],
)
def test_invalid_options(slot_type, options):
    with pytest.raises(DomainError) as excinfo:
        validate_slot_options(slot_type, options)
    assert excinfo.value.code == "invalid_options"


@pytest.mark.parametrize(
    ("slot_type", "options", "value", "expected"),
    [
        (Type.TEXT, {}, "hello", "hello"),
        (Type.RICHTEXT_BLOCKS, {}, [{"type": "paragraph"}], [{"type": "paragraph"}]),
        (Type.NUMBER, {"min": 0, "max": 10}, 7.5, 7.5),
        (Type.BOOL, {}, False, False),
        (Type.DATE, {}, "2026-09-28", "2026-09-28"),
        (Type.ENUM, {"choices": ["Beginner"]}, "Beginner", "Beginner"),
        (Type.URL, {}, "https://flarize.com/blog", "https://flarize.com/blog"),
        (Type.TEXT, {}, None, None),
    ],
)
def test_strict_values(slot_type, options, value, expected):
    assert coerce_value(slot(slot_type, **options), value) == expected


@pytest.mark.parametrize(
    ("slot_type", "options", "value"),
    [
        (Type.TEXT, {}, 5),
        (Type.RICHTEXT_BLOCKS, {}, "text"),
        (Type.RICHTEXT_BLOCKS, {}, ["text"]),
        (Type.NUMBER, {}, "5"),
        (Type.NUMBER, {}, True),
        (Type.NUMBER, {"max": 3}, 4),
        (Type.NUMBER, {"min": 3}, 2),
        (Type.BOOL, {}, "true"),
        (Type.DATE, {}, "28/09/2026"),
        (Type.DATE, {}, 20260928),
        (Type.ENUM, {"choices": ["a"]}, "b"),
        (Type.URL, {}, "ftp://flarize.com"),
        (Type.URL, {}, 5),
    ],
)
def test_strict_rejections(slot_type, options, value):
    with pytest.raises(ValueError):
        coerce_value(slot(slot_type, **options), value)


@pytest.mark.parametrize(
    ("slot_type", "value", "expected"),
    [
        (Type.TEXT, 5, "5"),
        (Type.NUMBER, "6", 6),
        (Type.NUMBER, "-2", -2),
        (Type.NUMBER, "2.5", 2.5),
        (Type.BOOL, "yes", True),
        (Type.BOOL, 0, False),
        (Type.DATE, "2026-09-28T10:00:00Z", "2026-09-28"),
    ],
)
def test_lenient_coercions_for_the_importer(slot_type, value, expected):
    assert coerce_value(slot(slot_type), value, lenient=True) == expected


@pytest.mark.parametrize(("slot_type", "value"), [(Type.NUMBER, "six"), (Type.BOOL, "maybe")])
def test_lenient_still_rejects_nonsense(slot_type, value):
    with pytest.raises(ValueError):
        coerce_value(slot(slot_type), value, lenient=True)
