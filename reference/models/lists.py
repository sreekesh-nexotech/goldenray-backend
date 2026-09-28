"""Calculator reference lists (PLAN §2.8), typed as the legacy tables: device types, wattages, room sizes, EVs."""

from __future__ import annotations

from django.db import models
from django.db.models import Q
from django.db.models.functions import Lower

from reference.models.base import ReferenceRow


class DeviceType(ReferenceRow):
    """Household devices for the advanced calculator (legacy ``device_types``)."""

    name = models.CharField(max_length=100)
    show_in_ui = models.BooleanField(default=True)
    url = models.CharField(max_length=255, blank=True, default="", help_text="Icon URL.")
    watts = models.PositiveIntegerField(null=True, blank=True)
    k_value = models.FloatField(null=True, blank=True, help_text="Usage factor applied by the advanced calculator.")

    class Meta:
        db_table = "reference_device_type"
        ordering = ["sort_order", "name", "id"]
        constraints = [
            # The advanced calculator looks devices up by name, case-insensitively.
            models.UniqueConstraint(Lower("name"), condition=Q(deleted_at__isnull=True), name="reference_device_type_name_live_uniq"),
            models.CheckConstraint(condition=Q(k_value__isnull=True) | Q(k_value__gte=0), name="reference_device_type_k_value_non_negative"),
        ]

    def __str__(self) -> str:
        return self.name


class Wattage(ReferenceRow):
    """Panel wattages offered on the comparison pages (legacy ``wattages``)."""

    value = models.PositiveIntegerField()
    show_in_ui = models.BooleanField(default=True)

    class Meta:
        db_table = "reference_wattage"
        ordering = ["sort_order", "value", "id"]
        constraints = [
            models.UniqueConstraint(fields=["value"], condition=Q(deleted_at__isnull=True), name="reference_wattage_value_live_uniq"),
            models.CheckConstraint(condition=Q(value__gte=1), name="reference_wattage_value_positive"),
        ]

    def __str__(self) -> str:
        return f"{self.value} Watts"


class RoomSize(ReferenceRow):
    """Home sizes and their typical monthly units (legacy ``room_size``)."""

    bhk_type = models.PositiveSmallIntegerField()
    size = models.PositiveIntegerField(help_text="Square feet.")
    units = models.PositiveIntegerField(help_text="Typical monthly consumption (kWh).")

    class Meta:
        db_table = "reference_room_size"
        ordering = ["sort_order", "bhk_type", "size", "id"]
        constraints = [
            models.UniqueConstraint(fields=["bhk_type", "size"], condition=Q(deleted_at__isnull=True), name="reference_room_size_live_uniq"),
            models.CheckConstraint(condition=Q(bhk_type__gte=1), name="reference_room_size_bhk_positive"),
            models.CheckConstraint(condition=Q(size__gte=1), name="reference_room_size_size_positive"),
        ]

    def __str__(self) -> str:
        return f"{self.bhk_type} BHK — {self.size} sq.ft"


class ElectricVehicle(ReferenceRow):
    """Shared columns of ``reference_ev_car`` / ``reference_ev_scooter`` (legacy ``ev_cars`` / ``ev_scooters``)."""

    model = models.CharField(max_length=100)
    battery_capacity = models.FloatField(help_text="kWh.")
    claimed_range = models.PositiveIntegerField(help_text="km.")
    adjusted_real_world_range = models.PositiveIntegerField(help_text="km.")
    ex_showroom_price = models.DecimalField(max_digits=14, decimal_places=2)
    energy_consumption = models.FloatField(null=True, blank=True, help_text="kWh per km.")
    k_value = models.FloatField(null=True, blank=True)

    class Meta:
        abstract = True

    def __str__(self) -> str:
        return self.model


def _ev_constraints(table: str) -> list:
    return [
        # The advanced calculator looks vehicles up by model name.
        models.UniqueConstraint(Lower("model"), condition=Q(deleted_at__isnull=True), name=f"{table}_model_live_uniq"),
        models.CheckConstraint(condition=Q(battery_capacity__gte=0), name=f"{table}_battery_non_negative"),
        models.CheckConstraint(condition=Q(ex_showroom_price__gte=0), name=f"{table}_price_non_negative"),
        models.CheckConstraint(condition=Q(energy_consumption__isnull=True) | Q(energy_consumption__gte=0), name=f"{table}_consumption_non_negative"),
        models.CheckConstraint(condition=Q(k_value__isnull=True) | Q(k_value__gte=0), name=f"{table}_k_value_non_negative"),
    ]


class EvCar(ElectricVehicle):
    class Meta:
        db_table = "reference_ev_car"
        ordering = ["sort_order", "model", "id"]
        constraints = _ev_constraints("reference_ev_car")


class EvScooter(ElectricVehicle):
    class Meta:
        db_table = "reference_ev_scooter"
        ordering = ["sort_order", "model", "id"]
        constraints = _ev_constraints("reference_ev_scooter")
