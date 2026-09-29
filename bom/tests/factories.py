from decimal import Decimal

import factory

from bom.models import FixedItem, PackageProfile, Slot, StructureItemType, StructureTemplate, StructureTemplateItem, Template, TubeWeight
from catalog.tests.factories import CategoryFactory


class TemplateFactory(factory.django.DjangoModelFactory):
    class Meta:
        model = Template
        django_get_or_create = ("system_type",)  # one live template per system type

    system_type = "ONGRID"
    name = "On-Grid"
    sizes = factory.LazyFunction(lambda: [{"key": "3", "label": "3 kW"}, {"key": "5tp", "label": "5kW 3P"}])
    three_phase_sizes = factory.LazyFunction(lambda: ["5tp"])
    tiers = factory.LazyFunction(lambda: ["base", "value", "premium"])


class SlotFactory(factory.django.DjangoModelFactory):
    class Meta:
        model = Slot

    template = factory.SubFactory(TemplateFactory)
    key = factory.Sequence(lambda n: f"slot_{n}")
    category = factory.SubFactory(CategoryFactory)
    qty_rule = factory.LazyFunction(lambda: {"type": "size_table", "qty": {"3": 1, "5tp": 2}})
    label = "Slot"
    gst_rate = Decimal("0.1800")


class FixedItemFactory(factory.django.DjangoModelFactory):
    class Meta:
        model = FixedItem

    template = factory.SubFactory(TemplateFactory)
    name = factory.Sequence(lambda n: f"Consumable {n}")
    unit_price = Decimal("10.00")
    gst_rate = Decimal("0.1800")
    qty_rule = factory.LazyFunction(lambda: {"type": "size_table", "bat_lookup": "always", "qty": {"3": 2, "5tp": 3}})


class StructureTemplateFactory(factory.django.DjangoModelFactory):
    class Meta:
        model = StructureTemplate

    slug = factory.Sequence(lambda n: f"roof_{n}")
    name = factory.Sequence(lambda n: f"Roof {n}")


class StructureItemFactory(factory.django.DjangoModelFactory):
    class Meta:
        model = StructureTemplateItem

    template = factory.SubFactory(StructureTemplateFactory)
    name = factory.Sequence(lambda n: f"Tube {n}")
    item_type = StructureItemType.TUBE
    tube_size = "2x1"
    weight_kg = Decimal("10.2000")
    qty_rule = factory.LazyFunction(lambda: {"type": "kw_interpolated", "points": {"3": 3, "5": 4}})


class TubeWeightFactory(factory.django.DjangoModelFactory):
    class Meta:
        model = TubeWeight

    tube_size = factory.Sequence(lambda n: f"{n}x1")
    weight_kg = Decimal("9.0000")


class PackageProfileFactory(factory.django.DjangoModelFactory):
    class Meta:
        model = PackageProfile

    key = factory.Sequence(lambda n: f"profile_{n}")
    label = "Profile"
    structure_material = "GI"
    inverter_type = "ONGRID"
