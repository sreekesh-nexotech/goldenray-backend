"""Website shapes: ``job-positions`` (legacy CMS delivery contract) and the ``job-applications`` form.

``PublicJobApplicationSerializer`` accepts the legacy form fields under their legacy names and applies the legacy
rules (``goldenray.serializers.job_application_serializer``) exactly; it maps them to the PLAN column names.
"""

from __future__ import annotations

from rest_framework import serializers

from careers.models import GENERAL_APPLICATION, JobApplication
from careers.services import validation

# ----------------------------------------------------------------------------------------------------------------
# Job positions (documentation shapes; the payload is built by careers.services.public)
# ----------------------------------------------------------------------------------------------------------------


class PublicJobCardSerializer(serializers.Serializer):
    uid = serializers.UUIDField(help_text="The posting's id (the legacy integer `id`); send it back as the application's `position_id`.")
    slug = serializers.CharField()
    title = serializers.CharField()
    department = serializers.CharField(allow_null=True)
    location = serializers.CharField()
    employment_type = serializers.CharField(help_text="Display label, e.g. 'Full-time'.")
    experience_required = serializers.CharField(allow_blank=True)
    application_deadline = serializers.DateField(allow_null=True)
    published_at = serializers.DateTimeField(allow_null=True)
    is_open = serializers.BooleanField()


class PublicDepartmentSerializer(serializers.Serializer):
    name = serializers.CharField()
    slug = serializers.CharField()


class PublicJobListMetaSerializer(serializers.Serializer):
    count = serializers.IntegerField()
    departments = PublicDepartmentSerializer(many=True)
    accepting_general_applications = serializers.BooleanField()
    intro = serializers.CharField(allow_blank=True)


class PublicJobListSerializer(serializers.Serializer):
    data = PublicJobCardSerializer(many=True)
    meta = PublicJobListMetaSerializer()


class PublicJobSeoSerializer(serializers.Serializer):
    title = serializers.CharField()
    description = serializers.CharField(allow_blank=True)
    canonical_url = serializers.CharField(allow_blank=True)
    noindex = serializers.BooleanField()


class PublicJobDetailDataSerializer(PublicJobCardSerializer):
    description = serializers.CharField(allow_blank=True)
    responsibilities = serializers.ListField(child=serializers.CharField())
    requirements = serializers.ListField(child=serializers.CharField())
    benefits = serializers.ListField(child=serializers.CharField())
    application_instructions = serializers.CharField(allow_blank=True)
    seo = PublicJobSeoSerializer()


class PublicJobDetailMetaSerializer(serializers.Serializer):
    schema = serializers.DictField(allow_null=True, help_text="JobPosting JSON-LD.")


class PublicJobDetailSerializer(serializers.Serializer):
    data = PublicJobDetailDataSerializer()
    meta = PublicJobDetailMetaSerializer()


# ----------------------------------------------------------------------------------------------------------------
# Job application form (multipart)
# ----------------------------------------------------------------------------------------------------------------
def _choice(choices, **kwargs):
    return serializers.ChoiceField(choices=choices, required=False, allow_blank=True, default="", **kwargs)


URL_MAX_LENGTH = 300  # careers_job_application.linkedin / .portfolio_website


def _fits_column(url: str) -> str:
    """``https://`` is added to a scheme-less URL; the result must still fit the column (else a 500, as in legacy)."""
    if len(url) > URL_MAX_LENGTH:
        raise serializers.ValidationError(f"Ensure this field has no more than {URL_MAX_LENGTH} characters.")
    return url


class PublicJobApplicationSerializer(serializers.Serializer):
    position = serializers.CharField(max_length=200, required=False, default=GENERAL_APPLICATION, help_text="Free-text position ('General application').")
    position_id = serializers.UUIDField(required=False, allow_null=True, default=None, help_text="uid of the posting applied for (job-positions `uid`).")
    position_title = serializers.CharField(max_length=200, required=False, allow_blank=True, default="")
    department_name = serializers.CharField(max_length=120, required=False, allow_blank=True, default="")
    full_name = serializers.CharField(max_length=255)
    email = serializers.EmailField(max_length=254)
    phone = serializers.CharField(max_length=20, help_text="10-digit Indian mobile; +91/91, spaces and dashes are accepted.")
    location = serializers.CharField(max_length=255)
    linkedin = serializers.CharField(max_length=URL_MAX_LENGTH, help_text="linkedin.com URL; https:// is added when missing (at most 300 characters with it).")
    portfolio_website = serializers.CharField(max_length=URL_MAX_LENGTH, required=False, allow_blank=True, default="", help_text="https:// is added when missing (at most 300 characters with it).")
    current_company = serializers.CharField(max_length=255, required=False, allow_blank=True, default="")
    current_role = serializers.CharField(max_length=255, required=False, allow_blank=True, default="")
    total_experience = _choice(JobApplication.Experience.choices)
    relevant_experience = _choice(JobApplication.Experience.choices)
    current_salary = _choice(JobApplication.Salary.choices)
    expected_salary = _choice(JobApplication.Salary.choices)
    notice_period = _choice(JobApplication.NoticePeriod.choices)
    heard_about_us = _choice(JobApplication.HeardAbout.choices)
    availability = serializers.CharField(max_length=32, required=False, allow_blank=True, default="")
    cover_note = serializers.CharField(max_length=validation.COVER_LETTER_MAX, required=False, allow_blank=True, default="", help_text="'Why Flarize?' answer.")
    resume = serializers.FileField(help_text="PDF, DOC or DOCX, at most 10 MB (type checked from the content).")
    portfolio_file = serializers.FileField(required=False, allow_null=True, default=None, help_text="Optional; same rules as the resume.")
    declaration_accepted = serializers.BooleanField(help_text="Must be true.")
    website = serializers.CharField(max_length=255, required=False, allow_blank=True, default="", write_only=True, help_text="Honeypot: leave empty.")

    def validate_full_name(self, value):
        value = value.strip()
        if not value:
            raise serializers.ValidationError("Full name is required.")
        return value

    def validate_email(self, value):
        return value.strip().lower()

    def validate_phone(self, value):
        mobile = validation.indian_mobile(value)
        if mobile is None:
            raise serializers.ValidationError("Enter a valid 10-digit Indian mobile number.")
        return validation.to_e164(mobile)

    def validate_location(self, value):
        value = value.strip()
        if not value:
            raise serializers.ValidationError("Current location is required.")
        return value

    def validate_linkedin(self, value):
        url = validation.linkedin_url(value)
        if url is None:
            raise serializers.ValidationError("Enter a valid LinkedIn URL (e.g. linkedin.com/in/username).")
        return _fits_column(url)

    def validate_portfolio_website(self, value):
        return _fits_column(validation.website_url(value))

    def _file(self, value):
        if value is None:
            return value
        error = validation.upload_error(value.name, value.size)
        if error:
            raise serializers.ValidationError(error)
        return value

    def validate_resume(self, value):
        return self._file(value)

    def validate_portfolio_file(self, value):
        return self._file(value)

    def validate_declaration_accepted(self, value):
        if not value:
            raise serializers.ValidationError("Please accept the declaration to continue.")
        return value

    def validate(self, attrs):
        if attrs.pop("website", ""):  # honeypot: bots fill every field
            raise serializers.ValidationError("Invalid submission.")
        return attrs

    def to_service(self) -> tuple[dict, object, object]:
        """``(data, resume, portfolio)`` in the service's (PLAN) vocabulary."""
        data = dict(self.validated_data)
        resume = data.pop("resume")
        portfolio = data.pop("portfolio_file", None)
        data["name"] = data.pop("full_name")
        data["phone_e164"] = data.pop("phone")
        data["cover_letter"] = data.pop("cover_note", "")
        data["position_label"] = data.pop("position", GENERAL_APPLICATION)
        data["position_uid"] = data.pop("position_id", None)
        return data, resume, portfolio


class JobApplicationReceiptSerializer(serializers.Serializer):
    uid = serializers.UUIDField()
    display_position = serializers.CharField()
    created_at = serializers.DateTimeField()
    message = serializers.CharField()
