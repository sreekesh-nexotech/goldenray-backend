"""Shapes of ``pricing/offers/``."""

from rest_framework import serializers

from core.serializers.common import ExpectedVersionMixin
from pricing.models import Offer, OfferTransition


class OfferTransitionSerializer(serializers.ModelSerializer):
    by = serializers.SerializerMethodField(help_text="User uid, or the source actor id (SYSTEM, Flarize ids) when not a platform user.")

    class Meta:
        model = OfferTransition
        fields = ["from_status", "to_status", "at", "by", "reason"]
        read_only_fields = fields

    def get_by(self, transition) -> str:
        return str(transition.by.uid) if transition.by_id else transition.by_label


class OfferSerializer(serializers.ModelSerializer):
    transitions = OfferTransitionSerializer(many=True, read_only=True)

    class Meta:
        model = Offer
        fields = [
            "uid",
            "code",
            "name",
            "type",
            "value",
            "applies_to_system",
            "applies_to_tier",
            "applies_to_size_key",
            "applies_to_size_kw",
            "starts_on",
            "ends_on",
            "status",
            "status_changed_at",
            "print_on_quotation",
            "stackable",
            "description",
            "content_version",
            "transitions",
            "created_at",
            "updated_at",
            "version",
        ]
        read_only_fields = fields


class OfferWriteSerializer(serializers.Serializer):
    code = serializers.RegexField(r"^[A-Za-z0-9][A-Za-z0-9_.-]{0,49}$", max_length=50, required=False, allow_blank=True, help_text="Generated (OFFER-<n>) when blank.")
    name = serializers.CharField(max_length=120)
    type = serializers.ChoiceField(choices=Offer._meta.get_field("type").choices)
    value = serializers.DecimalField(max_digits=12, decimal_places=2, min_value=0, help_text="₹ for FLAT, percent (≤ 100) for PERCENT.")
    applies_to_system = serializers.ChoiceField(choices=Offer._meta.get_field("applies_to_system").choices, required=False)
    applies_to_tier = serializers.ChoiceField(choices=Offer._meta.get_field("applies_to_tier").choices, required=False)
    applies_to_size_key = serializers.CharField(max_length=8, required=False, allow_blank=True, help_text="3, 5sp, 5tp … (blank: every size).")
    starts_on = serializers.DateField(required=False, allow_null=True)
    ends_on = serializers.DateField(required=False, allow_null=True)
    print_on_quotation = serializers.BooleanField(required=False)
    stackable = serializers.BooleanField(required=False)
    description = serializers.CharField(required=False, allow_blank=True)

    def to_representation(self, instance):
        return OfferSerializer(instance, context=self.context).data


class OfferUpdateSerializer(ExpectedVersionMixin, OfferWriteSerializer):
    code = None
    name = serializers.CharField(max_length=120, required=False)
    type = serializers.ChoiceField(choices=Offer._meta.get_field("type").choices, required=False)
    value = serializers.DecimalField(max_digits=12, decimal_places=2, min_value=0, required=False)
