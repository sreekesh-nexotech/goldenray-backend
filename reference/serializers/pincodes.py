"""Pincode shapes: staff (pincode + nested post offices) and the public lookup."""

from __future__ import annotations

from django.core.validators import RegexValidator
from drf_spectacular.utils import extend_schema_field
from rest_framework import serializers

from core.serializers import ExpectedVersionMixin
from reference.models import Pincode, PincodeOffice
from reference.models.pincode import PINCODE_REGEX
from reference.services.pincodes import pincodes_queryset

PINCODE = RegexValidator(PINCODE_REGEX, "Use the 6-digit Indian pincode.")


class PincodeOfficeSerializer(serializers.ModelSerializer):
    class Meta:
        model = PincodeOffice
        fields = ["uid", "office_name", "district", "state", "region", "division", "sort_order"]
        read_only_fields = fields


class PincodeSerializer(serializers.ModelSerializer):
    offices = serializers.SerializerMethodField()

    class Meta:
        model = Pincode
        fields = ["uid", "pincode", "district", "state", "serviceable", "distance_km_from_office", "is_active", "sort_order", "offices", "created_at", "updated_at", "version"]
        read_only_fields = fields

    @extend_schema_field(PincodeOfficeSerializer(many=True))
    def get_offices(self, pincode) -> list[dict]:
        offices = sorted((office for office in pincode.offices.all() if office.deleted_at is None), key=lambda office: (office.sort_order, office.id))
        return PincodeOfficeSerializer(offices, many=True).data


class PincodeOfficeWriteSerializer(serializers.Serializer):
    uid = serializers.UUIDField(required=False, help_text="Existing office to update; omit to add one.")
    office_name = serializers.CharField(max_length=100)
    district = serializers.CharField(max_length=100, required=False, allow_blank=True, default="")
    state = serializers.CharField(max_length=100, required=False, allow_blank=True, default="")
    region = serializers.CharField(max_length=100, required=False, allow_blank=True, default="")
    division = serializers.CharField(max_length=100, required=False, allow_blank=True, default="")
    sort_order = serializers.IntegerField(required=False, min_value=-1000000, max_value=1000000)


class _PincodeWriteSerializer(serializers.Serializer):
    pincode = serializers.CharField(max_length=6, validators=[PINCODE])
    district = serializers.CharField(max_length=100, required=False, allow_blank=True, help_text="Defaults to the first office's district.")
    state = serializers.CharField(max_length=100, required=False, allow_blank=True, help_text="Defaults to the first office's state.")
    serviceable = serializers.BooleanField(required=False, default=True)
    distance_km_from_office = serializers.DecimalField(max_digits=7, decimal_places=2, min_value=0, required=False, allow_null=True)
    is_active = serializers.BooleanField(required=False, default=True)
    sort_order = serializers.IntegerField(required=False, default=0, min_value=-1000000, max_value=1000000)
    offices = PincodeOfficeWriteSerializer(many=True, required=False, help_text="Replaces the office list (items with a uid update that office).")

    def to_representation(self, instance):
        return PincodeSerializer(pincodes_queryset().get(pk=instance.pk), context=self.context).data


class PincodeCreateSerializer(_PincodeWriteSerializer):
    pass


class PincodeUpdateSerializer(ExpectedVersionMixin, _PincodeWriteSerializer):
    pincode = serializers.CharField(max_length=6, validators=[PINCODE], required=False)
    serviceable = serializers.BooleanField(required=False)
    is_active = serializers.BooleanField(required=False)
    sort_order = serializers.IntegerField(required=False, min_value=-1000000, max_value=1000000)


class PublicPincodeOfficeSerializer(serializers.ModelSerializer):
    class Meta:
        model = PincodeOffice
        fields = ["office_name", "district", "state", "region", "division"]
        read_only_fields = fields


class PublicPincodeSerializer(serializers.ModelSerializer):
    offices = PublicPincodeOfficeSerializer(many=True, read_only=True)

    class Meta:
        model = Pincode
        fields = ["pincode", "district", "state", "serviceable", "distance_km_from_office", "offices"]
        read_only_fields = fields
