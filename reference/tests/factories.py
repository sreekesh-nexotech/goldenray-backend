from decimal import Decimal

import factory

from reference.models import Appliance, DeviceType, EvCar, EvScooter, KsebTariff, Pincode, PincodeOffice, RoomSize, Wattage


class PincodeFactory(factory.django.DjangoModelFactory):
    class Meta:
        model = Pincode

    pincode = factory.Sequence(lambda n: f"{680000 + n}")
    district = "ALAPPUZHA"
    state = "KERALA"


class PincodeOfficeFactory(factory.django.DjangoModelFactory):
    class Meta:
        model = PincodeOffice

    pincode = factory.SubFactory(PincodeFactory)
    office_name = factory.Sequence(lambda n: f"Office {n} BO")
    district = "ALAPPUZHA"
    state = "KERALA"
    region = "Kochi Region"
    division = "Alleppey Division"


class KsebTariffFactory(factory.django.DjangoModelFactory):
    class Meta:
        model = KsebTariff

    slab_from_units = factory.Sequence(lambda n: n * 100)
    slab_to_units = factory.LazyAttribute(lambda tariff: tariff.slab_from_units + 99)
    rate_per_unit = Decimal("6.7500")


class DeviceTypeFactory(factory.django.DjangoModelFactory):
    class Meta:
        model = DeviceType

    name = factory.Sequence(lambda n: f"Device {n}")
    watts = 100
    k_value = 1.0


class WattageFactory(factory.django.DjangoModelFactory):
    class Meta:
        model = Wattage

    value = factory.Sequence(lambda n: 10 + n)


class RoomSizeFactory(factory.django.DjangoModelFactory):
    class Meta:
        model = RoomSize

    bhk_type = 2
    size = factory.Sequence(lambda n: 800 + n * 100)
    units = 180


class EvCarFactory(factory.django.DjangoModelFactory):
    class Meta:
        model = EvCar

    model = factory.Sequence(lambda n: f"Car {n}")
    battery_capacity = 30.2
    claimed_range = 312
    adjusted_real_world_range = 212
    ex_showroom_price = Decimal("1249000.00")
    energy_consumption = 0.152
    k_value = 1.0


class EvScooterFactory(EvCarFactory):
    class Meta:
        model = EvScooter

    model = factory.Sequence(lambda n: f"Scooter {n}")


class ApplianceFactory(factory.django.DjangoModelFactory):
    class Meta:
        model = Appliance

    code = factory.Sequence(lambda n: f"appliance_{n}")
    name = factory.Sequence(lambda n: f"Appliance {n}")
    watts = 60
    default_hours = Decimal("5.00")
