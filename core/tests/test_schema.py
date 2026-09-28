"""The OpenAPI schema is the Studio contract: it must generate and validate with zero warnings."""

import pytest
from django.core.management import call_command


@pytest.mark.django_db
def test_schema_generates_without_warnings_and_validates(tmp_path):
    target = tmp_path / "schema.yaml"
    call_command("spectacular", "--validate", "--fail-on-warn", "--api-version", "v1", "--file", str(target))
    text = target.read_text()
    assert "/api/v1/settings/flags/" in text
    assert "staffJWT" in text
