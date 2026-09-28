"""The SeoFields mixin is shared by blog, sitepages, faqs and careers; lock its contract."""

from seo import schema
from seo.models import DESCRIPTION_MAX, TITLE_MAX, SchemaType, SeoFields


def test_mixin_is_abstract_and_has_expected_fields():
    names = {f.name for f in SeoFields._meta.get_fields()}
    assert SeoFields._meta.abstract
    assert {"seo_title", "meta_description", "canonical_url", "og_title", "og_description", "og_image", "schema_type", "schema_extra", "noindex"} <= names


def test_seo_issues_flags_missing_and_long_values():
    rec = SeoFields.__new__(SeoFields)
    rec.__dict__.update(seo_title="", meta_description="", noindex=False, title="")
    issues = SeoFields.seo_issues(rec)
    assert {i["field"] for i in issues} == {"seo_title", "meta_description"}
    assert SeoFields.seo_status(rec) == "error"

    rec.__dict__.update(seo_title="x" * (TITLE_MAX + 1), meta_description="y" * (DESCRIPTION_MAX + 1))
    levels = {i["level"] for i in SeoFields.seo_issues(rec)}
    assert levels == {"warning"}


def test_schema_choices_and_clean_helper():
    assert SchemaType.FAQ_PAGE == "FAQPage"
    assert schema._clean({"a": None, "b": {"c": ""}, "d": [None, 1]}) == {"d": [1]}
