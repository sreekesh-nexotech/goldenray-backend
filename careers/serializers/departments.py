"""Department shapes (staff ``careers/departments/``)."""

from __future__ import annotations

from django.core.validators import RegexValidator
from rest_framework import serializers

from careers.models import Department
from careers.models.department import SLUG_REGEX
from careers.services.departments import departments_queryset
from core.serializers import ExpectedVersionMixin

SLUG = RegexValidator(SLUG_REGEX, "Use lower-case letters, digits and single hyphens.")


class DepartmentSerializer(serializers.ModelSerializer):
    job_count = serializers.IntegerField(read_only=True, help_text="Live positions, archived ones included (they block deletion).")
    open_job_count = serializers.IntegerField(read_only=True, help_text="Published positions.")

    class Meta:
        model = Department
        fields = ["uid", "name", "slug", "description", "is_active", "sort_order", "job_count", "open_job_count", "created_at", "updated_at", "version"]
        read_only_fields = fields


class _DepartmentWriteSerializer(serializers.Serializer):
    name = serializers.CharField(max_length=120)
    slug = serializers.CharField(max_length=120, required=False, allow_blank=True, validators=[SLUG], help_text="Derived from the name when blank.")
    description = serializers.CharField(required=False, allow_blank=True, default="")
    is_active = serializers.BooleanField(required=False, default=True)
    sort_order = serializers.IntegerField(required=False, default=0, min_value=-100000, max_value=100000)

    def to_representation(self, instance):
        return DepartmentSerializer(departments_queryset().get(pk=instance.pk), context=self.context).data


class DepartmentCreateSerializer(_DepartmentWriteSerializer):
    pass


class DepartmentUpdateSerializer(ExpectedVersionMixin, _DepartmentWriteSerializer):
    name = serializers.CharField(max_length=120, required=False)
    description = serializers.CharField(required=False, allow_blank=True)
    is_active = serializers.BooleanField(required=False)
    sort_order = serializers.IntegerField(required=False, min_value=-100000, max_value=100000)
