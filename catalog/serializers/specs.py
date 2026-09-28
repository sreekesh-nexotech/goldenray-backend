"""Spec shapes (``panel_spec`` / ``inverter_spec`` / ``battery_spec`` / ``structure_spec``).

Read serializers return every column; write serializers make every column optional (a spec is patched field by
field; the service requires ``wattage_w`` / ``kw`` + ``inverter_type`` when the spec is first created).
"""

from rest_framework import serializers

from catalog.models import BatteryFamily, BatterySpec, InverterSpec, PanelSpec, StructureSpec
from catalog.services.specs import spec_fields


def _nullable_strings(help_text: str) -> serializers.ListField:
    return serializers.ListField(child=serializers.CharField(max_length=120), required=False, allow_null=True, max_length=200, help_text=help_text)


class PanelSpecSerializer(serializers.ModelSerializer):
    certifications = serializers.ListField(child=serializers.CharField(), read_only=True)

    class Meta:
        model = PanelSpec
        fields = spec_fields("panel")
        read_only_fields = fields


class BatteryFamilyRefSerializer(serializers.ModelSerializer):
    class Meta:
        model = BatteryFamily
        fields = ["uid", "slug", "name", "voltage_class"]
        read_only_fields = fields


class InverterSpecSerializer(serializers.ModelSerializer):
    certifications = serializers.ListField(child=serializers.CharField(), read_only=True)
    communication = serializers.ListField(child=serializers.CharField(), read_only=True)
    compatible_battery_families = serializers.ListField(child=serializers.SlugField(), read_only=True)

    class Meta:
        model = InverterSpec
        fields = spec_fields("inverter")
        read_only_fields = fields


class BatterySpecSerializer(serializers.ModelSerializer):
    family = BatteryFamilyRefSerializer(read_only=True, allow_null=True)
    compatible_inverters = serializers.ListField(child=serializers.CharField(), read_only=True, allow_null=True)
    compatible_system_types = serializers.ListField(child=serializers.CharField(), read_only=True, allow_null=True)
    compatible_phases = serializers.ListField(child=serializers.CharField(), read_only=True, allow_null=True)
    open_items = serializers.ListField(child=serializers.CharField(), read_only=True)
    status_history = serializers.ListField(child=serializers.DictField(), read_only=True)

    class Meta:
        model = BatterySpec
        fields = spec_fields("battery")
        read_only_fields = fields


class StructureSpecSerializer(serializers.ModelSerializer):
    class Meta:
        model = StructureSpec
        fields = spec_fields("structure")
        read_only_fields = fields


class PanelSpecWriteSerializer(serializers.ModelSerializer):
    certifications = serializers.ListField(child=serializers.CharField(max_length=120), required=False, max_length=50)

    class Meta:
        model = PanelSpec
        fields = spec_fields("panel")
        extra_kwargs = {name: {"required": False} for name in spec_fields("panel")}


class InverterSpecWriteSerializer(serializers.ModelSerializer):
    certifications = serializers.ListField(child=serializers.CharField(max_length=120), required=False, max_length=50)
    communication = serializers.ListField(child=serializers.CharField(max_length=60), required=False, max_length=20)
    compatible_battery_families = serializers.ListField(child=serializers.SlugField(max_length=64), required=False, max_length=50)

    class Meta:
        model = InverterSpec
        fields = spec_fields("inverter")
        extra_kwargs = {name: {"required": False} for name in spec_fields("inverter")}


class BatterySpecWriteSerializer(serializers.ModelSerializer):
    family = serializers.SlugRelatedField(slug_field="uid", queryset=BatteryFamily.objects.all(), required=False, allow_null=True, help_text="Battery family uid.")
    compatible_inverters = _nullable_strings("Inverter SKUs; null = not recorded.")
    compatible_system_types = _nullable_strings("e.g. ['hybrid']; null = not recorded.")
    compatible_phases = _nullable_strings("e.g. ['1P']; null = not recorded.")
    open_items = serializers.ListField(child=serializers.CharField(max_length=120), required=False, max_length=100)
    status_history = serializers.ListField(child=serializers.DictField(), required=False, max_length=200)

    class Meta:
        model = BatterySpec
        fields = spec_fields("battery")
        extra_kwargs = {name: {"required": False} for name in spec_fields("battery")}


class StructureSpecWriteSerializer(serializers.ModelSerializer):
    class Meta:
        model = StructureSpec
        fields = spec_fields("structure")
        extra_kwargs = {name: {"required": False} for name in spec_fields("structure")}


READ_SERIALIZERS = {"panel": PanelSpecSerializer, "inverter": InverterSpecSerializer, "battery": BatterySpecSerializer, "structure": StructureSpecSerializer}
WRITE_SERIALIZERS = {"panel": PanelSpecWriteSerializer, "inverter": InverterSpecWriteSerializer, "battery": BatterySpecWriteSerializer, "structure": StructureSpecWriteSerializer}
