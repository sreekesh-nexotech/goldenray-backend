"""The legacy CMS value rules, kept exactly (slug validator order and classes) plus the new key/route validators."""

import pytest
from django.core.exceptions import ValidationError

from blog.validators import slug_error, slug_error_code, validate_api_uid, validate_color, validate_component, validate_entry_slug, validate_key, validate_path_prefix


@pytest.mark.parametrize("slug", ["solar-panel-cost-in-kerala", "on-grid-vs-hybrid-kerala", "abc1", "a1b", "kw-3"])
def test_valid_slugs(slug):
    validate_entry_slug(slug)
    assert slug_error(slug) is None and slug_error_code(slug) is None


@pytest.mark.parametrize(
    ("slug", "code"),
    [
        (None, "slug_empty"),
        ("", "slug_empty"),
        (" solar-guide", "slug_whitespace"),
        ("x" * 256, "slug_too_long"),
        ("Not A Slug", "slug_malformed"),
        ("trailing-", "slug_malformed"),
        ("UPPER", "slug_malformed"),
        ("AB", "slug_malformed"),  # malformed wins over too short
        ("ab", "slug_too_short"),
        ("admin", "slug_reserved"),
        ("sitemap", "slug_reserved"),
        ("test", "slug_placeholder"),
        ("test-2", "slug_placeholder"),
        ("object-object", "slug_placeholder"),
        ("asdf-asdf", "slug_placeholder"),
        ("untitled", "slug_placeholder"),
    ],
)
def test_invalid_slugs(slug, code):
    with pytest.raises(ValidationError) as excinfo:
        validate_entry_slug(slug)
    assert excinfo.value.error_list[0].code == code
    assert slug_error(slug) and slug_error_code(slug) == code


@pytest.mark.parametrize(
    ("validator", "good", "bad"),
    [
        (validate_api_uid, ["articles", "case-studies"], ["Articles", "preview", "a_b", "", "x" * 81]),
        (validate_key, ["coverImg", "body_images", "a"], ["1abc", "cover-img", "", "x" * 61]),
        (validate_path_prefix, ["/blog", "/resources/case-studies"], ["blog", "/blog/", "/Blog", "", "//x"]),
        (validate_color, ["#ED8723", "#fff", "#12345678"], ["ED8723", "#12", "red"]),
        (validate_component, ["shared.rich-text", "blocks.cta"], ["rich-text", "Shared.Text", ""]),
    ],
)
def test_route_and_key_validators(validator, good, bad):
    for value in good:
        validator(value)
    for value in bad:
        with pytest.raises(ValidationError):
            validator(value)
