"""Job application shapes: staff ``careers/applications/`` (queue, detail, workflow actions)."""

from __future__ import annotations

from drf_spectacular.utils import extend_schema_field, extend_schema_serializer
from rest_framework import serializers

from accounts.models import User
from careers.models import JobApplication, JobApplicationEvent, JobApplicationNote, JobPosition
from core.serializers import ExpectedVersionMixin


# ``{uid, name}``; the component name leaves ``UserRef`` (``{uid, full_name}``) to the website pages.
@extend_schema_serializer(component_name="CareersUserRef")
class UserRefSerializer(serializers.Serializer):
    uid = serializers.UUIDField()
    name = serializers.CharField(source="get_full_name")


class StoredFileSerializer(serializers.Serializer):
    filename = serializers.CharField(source="original_filename")
    mime_type = serializers.CharField()
    size_bytes = serializers.IntegerField()


class JobApplicationListSerializer(serializers.ModelSerializer):
    position = serializers.SlugRelatedField(slug_field="uid", read_only=True)
    display_position = serializers.CharField(read_only=True)
    assignee = UserRefSerializer(read_only=True, allow_null=True)
    archived = serializers.BooleanField(source="is_archived", read_only=True)
    archived_at = serializers.DateTimeField(source="deleted_at", read_only=True, allow_null=True)
    has_resume = serializers.SerializerMethodField()
    has_portfolio = serializers.SerializerMethodField()

    class Meta:
        model = JobApplication
        fields = [
            "uid",
            "position",
            "position_label",
            "position_title",
            "department_name",
            "display_position",
            "status",
            "status_changed_at",
            "assignee",
            "source",
            "archived",
            "archived_at",
            "name",
            "email",
            "phone_e164",
            "location",
            "has_resume",
            "has_portfolio",
            "created_at",
            "updated_at",
            "version",
        ]
        read_only_fields = fields

    def get_has_resume(self, application) -> bool:
        return application.resume_id is not None

    def get_has_portfolio(self, application) -> bool:
        return application.portfolio_id is not None


class JobApplicationNoteSerializer(serializers.ModelSerializer):
    author = serializers.SlugRelatedField(source="created_by", slug_field="uid", read_only=True, allow_null=True)

    class Meta:
        model = JobApplicationNote
        fields = ["uid", "body", "author", "author_name", "created_at"]
        read_only_fields = fields


class JobApplicationEventSerializer(serializers.ModelSerializer):
    kind_label = serializers.CharField(source="get_kind_display", read_only=True)
    actor = serializers.SlugRelatedField(slug_field="uid", read_only=True, allow_null=True)

    class Meta:
        model = JobApplicationEvent
        fields = ["kind", "kind_label", "from_status", "to_status", "detail", "actor", "actor_name", "created_at"]
        read_only_fields = fields


class JobApplicationSerializer(JobApplicationListSerializer):
    resume = StoredFileSerializer(read_only=True, allow_null=True, help_text="Download through …/download/resume/.")
    portfolio = StoredFileSerializer(read_only=True, allow_null=True, help_text="Download through …/download/portfolio/.")
    allowed_transitions = serializers.SerializerMethodField()
    notes = serializers.SerializerMethodField()
    events = serializers.SerializerMethodField()

    DETAIL_ITEMS = 100  # newest notes/events embedded in the detail; the notes/ and events/ lists page through all

    class Meta(JobApplicationListSerializer.Meta):
        fields = JobApplicationListSerializer.Meta.fields + [
            "linkedin",
            "portfolio_website",
            "current_company",
            "current_role",
            "total_experience",
            "relevant_experience",
            "current_salary",
            "expected_salary",
            "notice_period",
            "heard_about_us",
            "availability",
            "cover_letter",
            "declaration_accepted",
            "resume",
            "portfolio",
            "allowed_transitions",
            "notes",
            "events",
        ]
        read_only_fields = fields

    @extend_schema_field(serializers.ListField(child=serializers.ChoiceField(choices=JobApplication.Status.choices)))
    def get_allowed_transitions(self, application) -> list:
        return [] if application.is_archived else application.allowed_transitions()

    @extend_schema_field(JobApplicationNoteSerializer(many=True))
    def get_notes(self, application) -> list:
        notes = application.notes.select_related("created_by").order_by("-created_at", "-id")[: self.DETAIL_ITEMS]
        return JobApplicationNoteSerializer(notes, many=True).data

    @extend_schema_field(JobApplicationEventSerializer(many=True))
    def get_events(self, application) -> list:
        events = application.events.select_related("actor").order_by("-created_at", "-id")[: self.DETAIL_ITEMS]
        return JobApplicationEventSerializer(events, many=True).data


class ApplicationStatusSerializer(ExpectedVersionMixin, serializers.Serializer):
    status = serializers.ChoiceField(choices=JobApplication.Status.choices)
    note = serializers.CharField(required=False, allow_blank=True, default="", max_length=255)


class ApplicationAssignSerializer(ExpectedVersionMixin, serializers.Serializer):
    position = serializers.SlugRelatedField(slug_field="uid", queryset=JobPosition.objects.all(), required=False, help_text="Posting uid to link (title/department are snapshotted).")
    assignee = serializers.SlugRelatedField(slug_field="uid", queryset=User.objects.all(), required=False, allow_null=True, help_text="User uid; null clears.")

    def validate(self, attrs):
        if "position" not in attrs and "assignee" not in attrs:
            raise serializers.ValidationError({"non_field_errors": ["Send a position, an assignee, or both."]})
        return attrs


class ApplicationNoteCreateSerializer(serializers.Serializer):
    body = serializers.CharField(max_length=5000)


class ApplicationActionSerializer(ExpectedVersionMixin, serializers.Serializer):
    pass


class ApplicationFileLinkSerializer(serializers.Serializer):
    url = serializers.CharField(help_text="Signed, 10-minute URL (media/download/<token>/); no Authorization header needed.")
    expires_at = serializers.DateTimeField(allow_null=True)
    filename = serializers.CharField()
    mime_type = serializers.CharField()
    size_bytes = serializers.IntegerField()
