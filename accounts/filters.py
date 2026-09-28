"""List filters for ``users/`` and ``roles/`` (``?field=`` or ``?filter[field]=``)."""

import django_filters

from accounts.models import Role, User


class UserFilter(django_filters.FilterSet):
    is_active = django_filters.BooleanFilter()
    must_reset_password = django_filters.BooleanFilter()
    role = django_filters.UUIDFilter(field_name="role__uid", help_text="Role uid.")

    class Meta:
        model = User
        fields: list[str] = []


class RoleFilter(django_filters.FilterSet):
    is_system = django_filters.BooleanFilter()

    class Meta:
        model = Role
        fields: list[str] = []
