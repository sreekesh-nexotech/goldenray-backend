from decimal import Decimal

import factory

from catalog.tests.factories import ComponentFactory
from inventory.models import Direction, Location, Movement, Reason


class OfficeFactory(factory.django.DjangoModelFactory):
    """An HR office row, named by app label: inventory (tests included) never imports hr (import-linter)."""

    class Meta:
        model = "hr.Office"

    code = factory.Sequence(lambda n: f"OF{n}")
    name = factory.Sequence(lambda n: f"Office {n}")
    timezone = "Asia/Kolkata"


class LocationFactory(factory.django.DjangoModelFactory):
    class Meta:
        model = Location

    code = factory.Sequence(lambda n: f"LOC-{n}")
    name = factory.Sequence(lambda n: f"Store {n}")


class MovementFactory(factory.django.DjangoModelFactory):
    """A raw ledger row (no balance check, no audit): for arranging state. Use the service to test behaviour."""

    class Meta:
        model = Movement

    component = factory.SubFactory(ComponentFactory)
    location = factory.SubFactory(LocationFactory)
    qty = Decimal("10.000")
    direction = Direction.IN
    reason = Reason.PURCHASE
