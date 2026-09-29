"""Customer-surface shapes (``/api/customer/v1/inspection-approvals/<token>/``): no internal remarks, no prices."""

from __future__ import annotations

from rest_framework import serializers

from site_inspections.models.choices import ApprovalStatus


class _ApprovalSiteSerializer(serializers.Serializer):
    address = serializers.CharField()
    pincode = serializers.CharField()
    district = serializers.CharField()


class _ApprovalSystemSerializer(serializers.Serializer):
    system_type = serializers.CharField()
    size_kw = serializers.DecimalField(max_digits=8, decimal_places=3, allow_null=True)
    phase = serializers.CharField(allow_null=True)
    panel = serializers.CharField(allow_null=True)
    inverter = serializers.CharField(allow_null=True)
    battery = serializers.CharField(allow_null=True)


class _ApprovalLocationSerializer(serializers.Serializer):
    annotation_type = serializers.CharField()
    photo_url = serializers.CharField(allow_null=True)
    photo_expires_at = serializers.DateTimeField(allow_null=True)
    geometry = serializers.DictField(allow_null=True)
    width_m = serializers.CharField(allow_null=True)
    height_m = serializers.CharField(allow_null=True)
    area_m2 = serializers.CharField(allow_null=True)


class ApprovalSummarySerializer(serializers.Serializer):
    inspection_number = serializers.CharField()
    approval_number = serializers.IntegerField()
    status = serializers.CharField()
    customer_name = serializers.CharField()
    phone_masked = serializers.CharField()
    expires_at = serializers.DateTimeField(allow_null=True)
    visit_date = serializers.DateField()
    site = _ApprovalSiteSerializer()
    system = _ApprovalSystemSerializer()
    locations = _ApprovalLocationSerializer(many=True)
    responded_at = serializers.DateTimeField(allow_null=True)


class OtpSentSerializer(serializers.Serializer):
    status = serializers.CharField()
    phone_masked = serializers.CharField()
    expires_at = serializers.DateTimeField()


class RespondSerializer(serializers.Serializer):
    code = serializers.RegexField(r"^[0-9]{4,10}$", help_text="The one-time code sent to the phone on the approval.")
    decision = serializers.ChoiceField(choices=[ApprovalStatus.APPROVED, ApprovalStatus.REJECTED])
    comment = serializers.CharField(required=False, allow_blank=True, max_length=2000)
    signature = serializers.FileField(required=False, allow_null=True, help_text="Optional drawn signature (PNG, ≤ 2 MB).")


class RespondedSerializer(serializers.Serializer):
    status = serializers.CharField()
    approval_number = serializers.IntegerField(source="number")
    responded_at = serializers.DateTimeField()
