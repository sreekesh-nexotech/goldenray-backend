"""Job position shapes (staff ``careers/positions/``)."""

from __future__ import annotations

from django.core.validators import RegexValidator
from drf_spectacular.utils import extend_schema_field
from rest_framework import serializers

from careers.models import Department, JobPosition
from careers.models.department import SLUG_REGEX
from careers.services.positions import positions_queryset, publish_errors
from core.serializers import ExpectedVersionMixin
from media.models import MediaAsset
from media.serializers import MediaAssetRefSerializer
from seo.models.fields import SchemaType

SLUG = RegexValidator(SLUG_REGEX, "Use lower-case letters, digits and single hyphens.")


class SeoIssueSerializer(serializers.Serializer):
    level = serializers.ChoiceField(choices=["error", "warning"])
    field = serializers.CharField()
    message = serializers.CharField()


class JobPositionListSerializer(serializers.ModelSerializer):
    department = serializers.SlugRelatedField(slug_field="uid", read_only=True)
    department_name = serializers.CharField(source="department.name", read_only=True)
    employment_type_label = serializers.CharField(source="get_employment_type_display", read_only=True)
    application_count = serializers.IntegerField(read_only=True, default=0, help_text="Live (not archived) applications.")
    seo_status = serializers.SerializerMethodField()

    class Meta:
        model = JobPosition
        fields = [
            "uid",
            "title",
            "slug",
            "department",
            "department_name",
            "location",
            "employment_type",
            "employment_type_label",
            "status",
            "sort_order",
            "application_count",
            "seo_status",
            "opens_on",
            "application_deadline",
            "openings",
            "published_at",
            "closed_at",
            "created_at",
            "updated_at",
            "version",
        ]
        read_only_fields = fields

    @extend_schema_field(serializers.ChoiceField(choices=["ok", "warning", "error"]))
    def get_seo_status(self, position) -> str:
        return position.seo_status()


class JobPositionSerializer(JobPositionListSerializer):
    og_image = MediaAssetRefSerializer(read_only=True, allow_null=True)
    seo_issues = serializers.SerializerMethodField()
    publish_errors = serializers.SerializerMethodField()
    public_url = serializers.SerializerMethodField(help_text="Site-relative path of the public page.")

    class Meta(JobPositionListSerializer.Meta):
        fields = JobPositionListSerializer.Meta.fields + [
            "experience_required",
            "description",
            "responsibilities",
            "requirements",
            "benefits",
            "application_instructions",
            "seo_title",
            "meta_description",
            "canonical_url",
            "og_title",
            "og_description",
            "og_image",
            "schema_type",
            "schema_extra",
            "noindex",
            "seo_issues",
            "publish_errors",
            "public_url",
        ]
        read_only_fields = fields

    @extend_schema_field(SeoIssueSerializer(many=True))
    def get_seo_issues(self, position) -> list:
        return position.seo_issues()

    @extend_schema_field(serializers.ListField(child=serializers.CharField()))
    def get_publish_errors(self, position) -> list:
        return publish_errors(position)

    def get_public_url(self, position) -> str:
        return position.seo_path()


class _JobPositionWriteSerializer(serializers.Serializer):
    title = serializers.CharField(max_length=200)
    slug = serializers.CharField(max_length=220, required=False, allow_blank=True, validators=[SLUG], help_text="Derived from the title when blank.")
    department = serializers.SlugRelatedField(slug_field="uid", queryset=Department.objects.all(), help_text="Department uid.")
    location = serializers.CharField(max_length=160)
    employment_type = serializers.ChoiceField(choices=JobPosition.EmploymentType.choices, required=False, default=JobPosition.EmploymentType.FULL_TIME)
    experience_required = serializers.CharField(max_length=120, required=False, allow_blank=True, default="")
    description = serializers.CharField(required=False, allow_blank=True, default="")
    responsibilities = serializers.CharField(required=False, allow_blank=True, default="", help_text="One per line.")
    requirements = serializers.CharField(required=False, allow_blank=True, default="", help_text="One per line.")
    benefits = serializers.CharField(required=False, allow_blank=True, default="", help_text="One per line.")
    application_instructions = serializers.CharField(required=False, allow_blank=True, default="")
    opens_on = serializers.DateField(required=False, allow_null=True, default=None)
    application_deadline = serializers.DateField(required=False, allow_null=True, default=None)
    openings = serializers.IntegerField(required=False, allow_null=True, default=None, min_value=1, max_value=32767)
    sort_order = serializers.IntegerField(required=False, default=0, min_value=-100000, max_value=100000)
    seo_title = serializers.CharField(max_length=255, required=False, allow_blank=True, default="")
    meta_description = serializers.CharField(required=False, allow_blank=True, default="")
    canonical_url = serializers.URLField(max_length=1000, required=False, allow_blank=True, default="")
    og_title = serializers.CharField(max_length=255, required=False, allow_blank=True, default="")
    og_description = serializers.CharField(required=False, allow_blank=True, default="")
    og_image = serializers.SlugRelatedField(slug_field="uid", queryset=MediaAsset.objects.all(), required=False, allow_null=True, default=None, help_text="Public IMAGE uid (null clears).")
    schema_type = serializers.ChoiceField(choices=SchemaType.choices, required=False, default=SchemaType.NONE)
    schema_extra = serializers.DictField(required=False, default=dict, help_text="Only the JobPosting fields the record cannot supply (e.g. baseSalary).")
    noindex = serializers.BooleanField(required=False, default=False)

    def validate_og_image(self, asset):
        if asset is not None and (not asset.is_public or asset.kind != MediaAsset.Kind.IMAGE):
            raise serializers.ValidationError("Use a public IMAGE from the media library.")
        return asset

    def to_representation(self, instance):
        return JobPositionSerializer(positions_queryset().get(pk=instance.pk), context=self.context).data


class JobPositionCreateSerializer(_JobPositionWriteSerializer):
    pass


class JobPositionUpdateSerializer(ExpectedVersionMixin, _JobPositionWriteSerializer):
    title = serializers.CharField(max_length=200, required=False)
    department = serializers.SlugRelatedField(slug_field="uid", queryset=Department.objects.all(), required=False, help_text="Department uid.")
    location = serializers.CharField(max_length=160, required=False)


class PositionActionSerializer(ExpectedVersionMixin, serializers.Serializer):
    pass


class PositionPreviewSerializer(serializers.Serializer):
    url = serializers.CharField()
    title = serializers.CharField()
    description = serializers.CharField(allow_blank=True)
    department = serializers.CharField(allow_null=True)
    location = serializers.CharField()
    employment_type = serializers.CharField()
    schema = serializers.DictField(allow_null=True, help_text="JobPosting JSON-LD as the website will embed it.")
    seo_issues = SeoIssueSerializer(many=True)
    publish_errors = serializers.ListField(child=serializers.CharField())


class OverviewCountsSerializer(serializers.Serializer):
    active_positions = serializers.IntegerField()
    draft_positions = serializers.IntegerField()
    closed_positions = serializers.IntegerField()
    departments = serializers.IntegerField()
    applications_total = serializers.IntegerField(required=False, help_text="Only with applications.view.")
    applications_new = serializers.IntegerField(required=False, help_text="Only with applications.view.")


class CareersOverviewSerializer(serializers.Serializer):
    counts = OverviewCountsSerializer()
    open_positions = JobPositionListSerializer(many=True)
