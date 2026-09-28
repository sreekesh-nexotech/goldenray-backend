"""Database rules the careers tables must enforce on their own (CLAUDE.md: every enum column has a CHECK)."""

import pytest
from django.db import IntegrityError, transaction

from careers.models import JobApplicationEvent, JobPosition
from careers.tests.factories import JobApplicationFactory, JobPositionFactory

pytestmark = pytest.mark.django_db


def refused(queryset, **values) -> bool:
    try:
        with transaction.atomic():
            queryset.update(**values)
    except IntegrityError:
        return True
    return False


def test_position_schema_type_is_an_enum_column():
    position = JobPositionFactory()
    assert refused(JobPosition.objects.filter(pk=position.pk), schema_type="Recipe")
    assert not refused(JobPosition.objects.filter(pk=position.pk), schema_type="JobPosting")


def test_event_statuses_are_application_statuses():
    event = JobApplicationEvent.objects.create(application=JobApplicationFactory(), kind=JobApplicationEvent.Kind.STATUS, from_status="NEW", to_status="SCREENING")
    events = JobApplicationEvent.objects.filter(pk=event.pk)
    assert refused(events, to_status="reviewing") and refused(events, from_status="selected")
    assert not refused(events, from_status="", to_status="")
