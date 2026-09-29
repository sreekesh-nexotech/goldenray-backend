"""Website shapes of the calculators (``/api/public/v1/calculators/basic|basic-v2|advanced/``).

The responses are the legacy payloads key for key (the numbers are the legacy binary64 results). The request
serializers document the inputs only: the endpoints read the JSON body as the legacy views did (a number or a
numeric text where a number is expected, the pincode as sent), so the engine — not DRF — validates them.
"""

from __future__ import annotations

from rest_framework import serializers


class BasicRequestSerializer(serializers.Serializer):
    monthly_bill = serializers.JSONField(help_text="Monthly electricity bill (₹). basic: a JSON number; basic-v2: a whole number or its text.")
    pincode = serializers.JSONField(help_text="Six-digit pincode (text or number); must be a known pincode.")
    property_type = serializers.JSONField(help_text="basic: echoed back. basic-v2: Residential or Commercial (any case).")


class BasicResponseSerializer(serializers.Serializer):
    estimated_units = serializers.FloatField()
    daily_consumption = serializers.FloatField()
    buffered_daily = serializers.FloatField()
    solar_capacity_kW = serializers.IntegerField()  # noqa: N815 - legacy key
    area_required = serializers.IntegerField(allow_null=True)
    installation_time_days = serializers.IntegerField(allow_null=True)
    total_cost = serializers.FloatField(allow_null=True)
    subsidy = serializers.FloatField(allow_null=True)
    pincode = serializers.JSONField()
    property_type = serializers.JSONField()


class EmiDetailsSerializer(serializers.Serializer):
    emi_per_month = serializers.FloatField()
    total_payment = serializers.FloatField()
    total_interest = serializers.FloatField(required=False, help_text="basic-v2 only.")


class GraphDatasetSerializer(serializers.Serializer):
    data = serializers.ListField(child=serializers.FloatField(), help_text="Cumulative spend at years 0, 5, 10, 15, 20, 25.")


class BasicV2ResponseSerializer(serializers.Serializer):
    solar_capacity_kW = serializers.FloatField()  # noqa: N815 - legacy key
    area_required = serializers.IntegerField()
    installation_time_days = serializers.CharField()
    total_cost = serializers.FloatField()
    subsidy = serializers.FloatField()
    final_cost = serializers.FloatField()
    interest_rate = serializers.FloatField()
    loan_available = serializers.CharField()
    emi_details = EmiDetailsSerializer()
    pincode = serializers.JSONField()
    property_type = serializers.JSONField()
    datasets = GraphDatasetSerializer(many=True, help_text="[without solar, with solar].")
    savings = serializers.FloatField()


class AdvancedSpecificationsSerializer(serializers.Serializer):
    home_type = serializers.CharField(required=False, help_text='"New Home" or "Existing Home".')
    grid_type = serializers.CharField(help_text='"On Grid" or "Hybrid".')
    bill_frequency = serializers.CharField(required=False, help_text='"Monthly" or "BI-Monthly" (default).')
    average_bill = serializers.JSONField(required=False, help_text="Existing home: average bill (₹).")
    estimated_base_load = serializers.JSONField(required=False, help_text="New home: estimated units.")


class AdvancedDeviceSerializer(serializers.Serializer):
    device_type = serializers.CharField(help_text="A device type name (any case) or Light.")
    no_of_units = serializers.JSONField(required=False)
    daily_usage = serializers.JSONField(required=False, help_text="Hours per day.")


class AdvancedVehicleSerializer(serializers.Serializer):
    model = serializers.CharField(help_text="An EV car or scooter model (exact name).")
    daily_avg_km = serializers.JSONField(required=False)
    no_of_vehicles = serializers.JSONField(required=False)


class AdvancedUsageSerializer(serializers.Serializer):
    usage_electronic_devices = AdvancedDeviceSerializer(many=True, required=False)
    electric_vehicles = AdvancedVehicleSerializer(many=True, required=False)


class AdvancedPreferenceSerializer(serializers.Serializer):
    backup_hours = serializers.JSONField(required=False)
    preference_electronic_devices = AdvancedDeviceSerializer(many=True, required=False)


class AdvancedRequestSerializer(serializers.Serializer):
    Specifications = AdvancedSpecificationsSerializer()  # noqa: N815 - legacy key
    usageDetails = AdvancedUsageSerializer(required=False)  # noqa: N815 - legacy key
    preferenceDetails = AdvancedPreferenceSerializer(required=False)  # noqa: N815 - legacy key


class AdvancedResponseSerializer(serializers.Serializer):
    bill_range = serializers.IntegerField()
    power_capacity = serializers.FloatField()
    time_to_complete = serializers.CharField()
    overall_setup_cost = serializers.FloatField()
    total_subsidy = serializers.FloatField()
    emi_details = EmiDetailsSerializer()
    area_required = serializers.IntegerField()
    loan_available = serializers.CharField()
    per_kw_rate = serializers.FloatField(allow_null=True)
    final_cost = serializers.FloatField(allow_null=True)
    interest_rate = serializers.FloatField(allow_null=True)
    type = serializers.CharField()
    default_backup_hours = serializers.IntegerField(required=False)
    battery_capacity = serializers.FloatField(required=False)
    battery_price = serializers.FloatField(required=False)
    inverter_price = serializers.FloatField(required=False)
    total_battery_cost = serializers.FloatField(required=False)
    calculated_required_capacity = serializers.FloatField(required=False)
    total_backup_watts = serializers.FloatField(required=False)
    average_load_kw = serializers.FloatField(required=False)
    actual_backup_time = serializers.FloatField(required=False)
    battery_info = serializers.CharField(required=False)
    graph_without_solar = serializers.ListField(child=serializers.IntegerField())
    graph_with_solar = serializers.ListField(child=serializers.FloatField())
    savings = serializers.FloatField()
