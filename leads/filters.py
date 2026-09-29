"""List filters for the staff ``leads/`` endpoints (``?field=`` or ``?filter[field]=``)."""

import django_filters

from leads.models import AffiliateApplication, CustomerInstallation, KeralaDistrict, Lead, WarrantyRequest


class _AssignedFilter(django_filters.FilterSet):
    assignee = django_filters.UUIDFilter(field_name="assignee__uid", help_text="Assignee (user) uid.")
    unassigned = django_filters.BooleanFilter(field_name="assignee", lookup_expr="isnull", help_text="true: nobody is assigned.")
    created_from = django_filters.IsoDateTimeFilter(field_name="created_at", lookup_expr="gte")
    created_to = django_filters.IsoDateTimeFilter(field_name="created_at", lookup_expr="lte")


class LeadFilter(_AssignedFilter):
    status = django_filters.MultipleChoiceFilter(choices=Lead.Status.choices)
    kind = django_filters.MultipleChoiceFilter(choices=Lead.Kind.choices)
    form = django_filters.MultipleChoiceFilter(choices=Lead.Form.choices)
    customer = django_filters.UUIDFilter(field_name="customer__uid", help_text="Customer uid.")
    verified = django_filters.BooleanFilter(field_name="otp_verified_at", lookup_expr="isnull", exclude=True, help_text="true: the phone was OTP-verified.")
    pincode = django_filters.CharFilter(field_name="pincode")

    class Meta:
        model = Lead
        fields: list[str] = []


class AffiliateFilter(_AssignedFilter):
    status = django_filters.MultipleChoiceFilter(choices=AffiliateApplication.Status.choices)
    profession = django_filters.ChoiceFilter(choices=AffiliateApplication.Profession.choices)
    district = django_filters.ChoiceFilter(choices=KeralaDistrict.choices)

    class Meta:
        model = AffiliateApplication
        fields: list[str] = []


class WarrantyFilter(_AssignedFilter):
    status = django_filters.MultipleChoiceFilter(choices=WarrantyRequest.Status.choices)
    issue_type = django_filters.ChoiceFilter(choices=WarrantyRequest.IssueType.choices)
    customer = django_filters.UUIDFilter(field_name="customer__uid")

    class Meta:
        model = WarrantyRequest
        fields: list[str] = []


class InstallationFilter(_AssignedFilter):
    status = django_filters.MultipleChoiceFilter(choices=CustomerInstallation.Status.choices)
    pincode = django_filters.CharFilter(field_name="pincode")
    district = django_filters.CharFilter(field_name="district", lookup_expr="iexact")
    is_showcase = django_filters.BooleanFilter()
    installed_from = django_filters.DateFilter(field_name="installed_on", lookup_expr="gte")
    installed_to = django_filters.DateFilter(field_name="installed_on", lookup_expr="lte")

    class Meta:
        model = CustomerInstallation
        fields: list[str] = []
