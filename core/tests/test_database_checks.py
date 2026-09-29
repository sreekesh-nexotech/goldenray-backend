"""The system checks that need a database (``check --database default``): plain ``manage.py check`` skips them, and a
failure there (e.g. models.E034, an index name over 30 characters) makes ``manage.py migrate`` refuse a fresh database."""

import pytest
from django.core import checks


@pytest.mark.django_db
def test_database_system_checks_pass():
    messages = [m for m in checks.run_checks(databases=["default"], include_deployment_checks=False) if m.level >= checks.WARNING and not m.is_silenced()]
    assert messages == []


def test_no_index_or_constraint_name_over_the_postgres_identifier_limit():
    from django.apps import apps

    too_long = []
    for model in apps.get_models(include_auto_created=True):
        too_long += [f"{model._meta.label}.{index.name}" for index in model._meta.indexes if len(index.name) > 30]
        too_long += [f"{model._meta.label}.{constraint.name}" for constraint in model._meta.constraints if len(constraint.name) > 63]
    assert too_long == []
