"""Staff shapes of the calculators' sizing tables (``calculators/capacity-sizes/``, ``calculators/bill-range-sizes/``).

``interest_rate`` is a fraction (``"0.0650"`` = 6.5 %), as stored. DRF's generated unique validators are dropped:
live uniqueness is the database's job and answers 409 ``<entity>_exists``.
"""

from __future__ import annotations

from decimal import Decimal

from rest_framework import serializers

from calculators.models import BillRangeSize, CapacitySize
from calculators.services.sizing import BILL_RANGE_SIZES, CAPACITY_SIZES
from core.serializers import ExpectedVersionMixin

BASE_FIELDS = ["version", "created_at", "updated_at"]
MONEY = {"min_value": Decimal("0")}
EXTRA_KWARGS = {
    CapacitySize: {"power_capacity_kw": {"min_value": Decimal("0.001"), "validators": []}, "total_cost": MONEY, "total_subsidy": MONEY},
    BillRangeSize: {
        "bill_range": {"min_value": 1},
        "power_capacity_kw": {"min_value": Decimal("0.001")},
        "total_cost": MONEY,
        "total_subsidy": MONEY,
        "per_kw_rate": MONEY,
        "final_cost": MONEY,
        "inverter_price": MONEY,
        "interest_rate": {"min_value": Decimal("0"), "max_value": Decimal("0.9999")},
    },
}


def _meta(model, fields, extra=None, read_only=False):
    attrs = {"model": model, "fields": fields, "validators": [], "extra_kwargs": extra or {}}
    if read_only:
        attrs["read_only_fields"] = fields
    return type("Meta", (), attrs)


def _build(model, fields: tuple[str, ...], prefix: str):
    read = type(f"{prefix}Serializer", (serializers.ModelSerializer,), {"Meta": _meta(model, ["uid", *fields, *BASE_FIELDS], read_only=True)})

    def to_representation(self, instance):
        return read(instance, context=self.context).data

    create = type(f"{prefix}CreateSerializer", (serializers.ModelSerializer,), {"Meta": _meta(model, list(fields), EXTRA_KWARGS[model]), "to_representation": to_representation})
    update = type(f"{prefix}UpdateSerializer", (ExpectedVersionMixin, create), {"Meta": _meta(model, [*fields, "expected_version"], EXTRA_KWARGS[model])})
    return read, create, update


SERIALIZERS = {
    CAPACITY_SIZES.key: _build(CapacitySize, CAPACITY_SIZES.fields, "CalculatorCapacitySize"),
    BILL_RANGE_SIZES.key: _build(BillRangeSize, BILL_RANGE_SIZES.fields, "CalculatorBillRangeSize"),
}
