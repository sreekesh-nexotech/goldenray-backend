"""Serializer fields shared by the leads endpoints."""

from __future__ import annotations

from rest_framework import serializers

from leads.models.choices import by_label


class LenientChoiceField(serializers.ChoiceField):
    """A choice field that also takes the legacy labels (``"Real Estate Agent"``) and any letter case (``"footer"``).

    OpenAPI documents the codes; old payloads reach the same codes without a mapping layer.
    """

    def __init__(self, choices_class, **kwargs):
        self._lookup = by_label(choices_class)
        super().__init__(choices=choices_class.choices, **kwargs)

    def to_internal_value(self, data):
        if isinstance(data, str) and data.strip():
            data = self._lookup.get(data.strip().casefold(), data)
        return super().to_internal_value(data)


class HoneypotMixin(serializers.Serializer):
    """``website`` must stay empty (a hidden field bots fill in); a filled one is 400 ``Invalid submission.``"""

    website = serializers.CharField(required=False, allow_blank=True, write_only=True, max_length=255, help_text="Honeypot: always send empty.")

    def validate(self, attrs):
        if attrs.pop("website", ""):
            raise serializers.ValidationError("Invalid submission.")
        return super().validate(attrs)
