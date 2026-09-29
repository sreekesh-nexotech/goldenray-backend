from decimal import Decimal

import factory

from catalog.models import (
    BatteryFamily,
    BatterySpec,
    BomRole,
    Brand,
    Category,
    Component,
    ComponentPublicProfile,
    ComponentStatus,
    ComponentTier,
    InverterSpec,
    PanelSpec,
    ProfileStatus,
    StructureSpec,
)


class BrandFactory(factory.django.DjangoModelFactory):
    class Meta:
        model = Brand

    name = factory.Sequence(lambda n: f"Brand {n}")
    slug = factory.Sequence(lambda n: f"brand-{n}")


class CategoryFactory(factory.django.DjangoModelFactory):
    class Meta:
        model = Category

    slug = factory.Sequence(lambda n: f"category-{n}")
    name = factory.Sequence(lambda n: f"Category {n}")
    bom_role = BomRole.MISC
    gst_rate = Decimal("0.1800")
    sku_prefix = factory.Sequence(lambda n: f"C{n:03d}")


def panel_category(**kwargs) -> Category:
    return CategoryFactory(slug=kwargs.pop("slug", "panel"), name="Solar Panel", bom_role=BomRole.MAIN_PANEL, gst_rate=Decimal("0.0500"), sku_prefix=kwargs.pop("sku_prefix", "PNL"), **kwargs)


def inverter_category(**kwargs) -> Category:
    return CategoryFactory(slug=kwargs.pop("slug", "inverter"), name="Inverter", bom_role=BomRole.MAIN_INVERTER, gst_rate=Decimal("0.0500"), sku_prefix=kwargs.pop("sku_prefix", "INV"), **kwargs)


def battery_category(**kwargs) -> Category:
    return CategoryFactory(slug=kwargs.pop("slug", "battery"), name="Battery", bom_role=BomRole.BATTERY, gst_rate=Decimal("0.1800"), sku_prefix=kwargs.pop("sku_prefix", "BAT"), **kwargs)


def structure_category(**kwargs) -> Category:
    return CategoryFactory(slug=kwargs.pop("slug", "structure"), name="Structure", bom_role=BomRole.STRUCTURE, sku_prefix=kwargs.pop("sku_prefix", "STR"), **kwargs)


class ComponentFactory(factory.django.DjangoModelFactory):
    class Meta:
        model = Component

    sku = factory.Sequence(lambda n: f"x{n}")
    category = factory.SubFactory(CategoryFactory)
    brand = factory.SubFactory(BrandFactory)
    brand_label = factory.LazyAttribute(lambda component: component.brand.name if component.brand else "")
    name = factory.Sequence(lambda n: f"Component {n}")
    status = ComponentStatus.ACTIVE


class ComponentTierFactory(factory.django.DjangoModelFactory):
    class Meta:
        model = ComponentTier

    component = factory.SubFactory(ComponentFactory)
    tier = "BASE"


class PanelSpecFactory(factory.django.DjangoModelFactory):
    class Meta:
        model = PanelSpec

    component = factory.SubFactory(ComponentFactory)
    wattage_w = 540
    efficiency_pct = Decimal("21.36")


class InverterSpecFactory(factory.django.DjangoModelFactory):
    class Meta:
        model = InverterSpec

    component = factory.SubFactory(ComponentFactory)
    kw = Decimal("5.000")
    inverter_type = "ONGRID"
    phase = "1P"


class BatteryFamilyFactory(factory.django.DjangoModelFactory):
    class Meta:
        model = BatteryFamily

    slug = factory.Sequence(lambda n: f"family-{n}")
    name = factory.Sequence(lambda n: f"Family {n}")
    voltage_class = "48V"


class BatterySpecFactory(factory.django.DjangoModelFactory):
    class Meta:
        model = BatterySpec

    component = factory.SubFactory(ComponentFactory)
    capacity_kwh = Decimal("5.12")


class StructureSpecFactory(factory.django.DjangoModelFactory):
    class Meta:
        model = StructureSpec

    component = factory.SubFactory(ComponentFactory)
    tube_size = "1.5x1.5"


class PublicProfileFactory(factory.django.DjangoModelFactory):
    class Meta:
        model = ComponentPublicProfile

    component = factory.SubFactory(ComponentFactory, is_public=True)
    slug = factory.Sequence(lambda n: f"product-{n}")
    headline = factory.Sequence(lambda n: f"Product {n}")
    summary = "A well-made product."
    kerala_climate_score = 90
    status = ProfileStatus.DRAFT


def panel(**kwargs) -> Component:
    """An ACTIVE panel component with its spec (category ``panel`` created on first use)."""
    category = kwargs.pop("category", None) or Category.objects.filter(slug="panel").first() or panel_category()
    spec = kwargs.pop("spec", {})
    component = ComponentFactory(category=category, **kwargs)
    PanelSpecFactory(component=component, **spec)
    return component


def inverter(**kwargs) -> Component:
    category = kwargs.pop("category", None) or Category.objects.filter(slug="inverter").first() or inverter_category()
    spec = kwargs.pop("spec", {})
    component = ComponentFactory(category=category, **kwargs)
    InverterSpecFactory(component=component, **spec)
    return component


def battery(**kwargs) -> Component:
    category = kwargs.pop("category", None) or Category.objects.filter(slug="battery").first() or battery_category()
    spec = kwargs.pop("spec", {})
    component = ComponentFactory(category=category, **kwargs)
    BatterySpecFactory(component=component, **spec)
    return component


def published(component: Component, **kwargs) -> ComponentPublicProfile:
    from django.utils import timezone

    component.is_public = True
    component.save(update_fields=["is_public"])
    return PublicProfileFactory(component=component, status=ProfileStatus.PUBLISHED, published_at=timezone.now(), **kwargs)
