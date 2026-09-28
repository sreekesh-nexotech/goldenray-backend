"""Customer shapes (staff). Phones are accepted in any common spelling and returned as E.164."""

from __future__ import annotations

from django.contrib.auth import get_user_model
from drf_spectacular.utils import extend_schema_field
from rest_framework import serializers

from core.serializers import ExpectedVersionMixin
from customers.models import Customer, CustomerNote
from customers.models.customer import PINCODE_RE
from customers.services.phones import InvalidPhone, normalise_phone


class UserRefSerializer(serializers.Serializer):
    uid = serializers.UUIDField(read_only=True)
    full_name = serializers.CharField(source="get_full_name", read_only=True)
    email = serializers.EmailField(read_only=True)


class PhoneField(serializers.CharField):
    """Any common spelling in, E.164 out. ``mobile_only`` restricts to Indian mobile numbers (website forms)."""

    def __init__(self, *, mobile_only: bool = False, **kwargs):
        self.mobile_only = mobile_only
        kwargs.setdefault("max_length", 32)
        super().__init__(**kwargs)

    def to_internal_value(self, data):
        value = super().to_internal_value(data)
        if value == "" and self.allow_blank:
            return ""
        try:
            return normalise_phone(value, mobile_only=self.mobile_only, regions=frozenset({"IN"}) if self.mobile_only else None)
        except InvalidPhone as exc:
            raise serializers.ValidationError(str(exc)) from None


class ActiveUserField(serializers.SlugRelatedField):
    """A live, active staff user by ``uid``."""

    def __init__(self, **kwargs):
        super().__init__(slug_field="uid", queryset=get_user_model().objects.filter(is_active=True), **kwargs)


class CustomerSerializer(serializers.ModelSerializer):
    phone = serializers.CharField(source="phone_e164", read_only=True)
    owner = UserRefSerializer(read_only=True, allow_null=True)
    lead_uid = serializers.SerializerMethodField()

    class Meta:
        model = Customer
        fields = [
            "uid",
            "code",
            "name",
            "phone",
            "alt_phone",
            "email",
            "address",
            "pincode",
            "district",
            "state",
            "location",
            "google_map_link",
            "latitude",
            "longitude",
            "current_bill",
            "bill_cycle",
            "source",
            "owner",
            "lead_uid",
            "created_at",
            "updated_at",
            "version",
        ]
        read_only_fields = fields

    @extend_schema_field(serializers.UUIDField(allow_null=True))
    def get_lead_uid(self, customer):
        return str(customer.lead.uid) if customer.lead_id else None


class _CustomerWriteSerializer(serializers.Serializer):
    name = serializers.CharField(max_length=255)
    phone = PhoneField(source="phone_e164", help_text="Any common spelling; stored as E.164 (region IN by default).")
    alt_phone = serializers.CharField(max_length=20, required=False, allow_blank=True)
    email = serializers.EmailField(max_length=254, required=False, allow_blank=True)
    address = serializers.CharField(max_length=2000, required=False, allow_blank=True)
    pincode = serializers.RegexField(PINCODE_RE, max_length=6, required=False, allow_blank=True, error_messages={"invalid": "Enter a 6-digit pincode."})
    district = serializers.CharField(max_length=100, required=False, allow_blank=True)
    state = serializers.CharField(max_length=100, required=False, allow_blank=True)
    location = serializers.CharField(max_length=120, required=False, allow_blank=True)
    google_map_link = serializers.URLField(max_length=500, required=False, allow_blank=True)
    latitude = serializers.DecimalField(max_digits=9, decimal_places=6, min_value=-90, max_value=90, required=False, allow_null=True)
    longitude = serializers.DecimalField(max_digits=9, decimal_places=6, min_value=-180, max_value=180, required=False, allow_null=True)
    current_bill = serializers.DecimalField(max_digits=10, decimal_places=2, min_value=0, required=False, allow_null=True)
    bill_cycle = serializers.ChoiceField(choices=[("", "Not set"), *Customer.BillCycle.choices], required=False, allow_blank=True)
    source = serializers.ChoiceField(choices=Customer.Source.choices, required=False)
    owner_uid = ActiveUserField(source="owner", required=False, allow_null=True, help_text="Another owner than yourself needs customers.manage.")

    def validate_email(self, value):
        return value.strip().lower()

    def validate_alt_phone(self, value):
        value = value.strip()
        if not value:
            return ""
        try:
            return normalise_phone(value)
        except InvalidPhone as exc:
            raise serializers.ValidationError(str(exc)) from None

    def to_representation(self, instance):
        return CustomerSerializer(instance, context=self.context).data


class CustomerCreateSerializer(_CustomerWriteSerializer):
    pass


class CustomerUpdateSerializer(ExpectedVersionMixin, _CustomerWriteSerializer):
    name = serializers.CharField(max_length=255, required=False)
    phone = PhoneField(source="phone_e164", required=False)


class CustomerMergeSerializer(ExpectedVersionMixin, serializers.Serializer):
    into_uid = serializers.UUIDField(help_text="The customer that survives; this one is merged into it and archived.")
    into_expected_version = serializers.IntegerField(min_value=1, required=False, help_text="The survivor's `version` the client last read.")


class CustomerNoteSerializer(serializers.ModelSerializer):
    author = UserRefSerializer(source="created_by", read_only=True, allow_null=True)

    class Meta:
        model = CustomerNote
        fields = ["uid", "body", "pinned", "author", "created_at", "updated_at", "version"]
        read_only_fields = fields


class CustomerNoteCreateSerializer(serializers.Serializer):
    body = serializers.CharField(max_length=10_000)
    pinned = serializers.BooleanField(required=False, default=False)

    def to_representation(self, instance):
        return CustomerNoteSerializer(instance, context=self.context).data


class CustomerNoteUpdateSerializer(ExpectedVersionMixin, serializers.Serializer):
    body = serializers.CharField(max_length=10_000, required=False)
    pinned = serializers.BooleanField(required=False)


class TimelineEntrySerializer(serializers.Serializer):
    at = serializers.DateTimeField()
    kind = serializers.CharField()
    title = serializers.CharField()
    object_type = serializers.CharField()
    object_uid = serializers.UUIDField(allow_null=True)
    data = serializers.DictField()


class TimelineSerializer(serializers.Serializer):
    results = TimelineEntrySerializer(many=True)
    next_before = serializers.DateTimeField(allow_null=True, help_text="Pass as `before` to read older entries; null at the end.")


class TimelineQuerySerializer(serializers.Serializer):
    before = serializers.DateTimeField(required=False)
    limit = serializers.IntegerField(min_value=1, max_value=200, required=False, default=50)
