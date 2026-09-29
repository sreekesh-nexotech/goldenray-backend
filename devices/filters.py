"""Staff list filters (``?field=`` or ``?filter[field]=``)."""

import django_filters
from django.db.models import Q

from devices.models import AdmsRequest, AdmsUnknownDevice, Agent, Device, ProtocolMapping, SyncLog


class DeviceFilter(django_filters.FilterSet):
    office = django_filters.UUIDFilter(field_name="office__uid", help_text="Office uid (where the device stands).")
    agent = django_filters.UUIDFilter(field_name="agent__uid", help_text="Agent uid.")
    unassigned = django_filters.BooleanFilter(field_name="agent", lookup_expr="isnull", help_text="Carried by no agent.")
    is_active = django_filters.BooleanFilter()
    adms_enabled = django_filters.BooleanFilter()
    identity_status = django_filters.ChoiceFilter(choices=Device.IdentityStatus.choices)
    protocol = django_filters.ChoiceFilter(choices=Device.Protocol.choices)

    class Meta:
        model = Device
        fields: list[str] = []


class AgentFilter(django_filters.FilterSet):
    office = django_filters.UUIDFilter(method="filter_office", help_text="Office uid: agents filed under it or serving a device standing in it.")
    is_active = django_filters.BooleanFilter()

    class Meta:
        model = Agent
        fields: list[str] = []

    def filter_office(self, queryset, name, value):
        return queryset.filter(Q(office__uid=value) | Q(devices__office__uid=value, devices__deleted_at__isnull=True)).distinct()


class SyncLogFilter(django_filters.FilterSet):
    sync_type = django_filters.ChoiceFilter(choices=SyncLog.Type.choices)
    status = django_filters.ChoiceFilter(choices=SyncLog.Status.choices)

    class Meta:
        model = SyncLog
        fields: list[str] = []


class ProtocolMappingFilter(django_filters.FilterSet):
    field = django_filters.ChoiceFilter(choices=ProtocolMapping.Field.choices)
    device_platform = django_filters.CharFilter()
    firmware_version = django_filters.CharFilter()
    confidence = django_filters.ChoiceFilter(choices=ProtocolMapping.Confidence.choices)

    class Meta:
        model = ProtocolMapping
        fields: list[str] = []


class AdmsRequestFilter(django_filters.FilterSet):
    serial = django_filters.CharFilter(field_name="device_serial", help_text="The serial the request claimed.")
    kind = django_filters.ChoiceFilter(field_name="request_kind", choices=AdmsRequest.Kind.choices)
    device = django_filters.UUIDFilter(field_name="device__uid", help_text="Resolved device uid.")

    class Meta:
        model = AdmsRequest
        fields: list[str] = []


class AdmsUnknownDeviceFilter(django_filters.FilterSet):
    reason = django_filters.ChoiceFilter(field_name="last_reason", choices=AdmsUnknownDevice.Reason.choices)

    class Meta:
        model = AdmsUnknownDevice
        fields: list[str] = []
