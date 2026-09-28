"""Media library filters (``?field=`` or ``?filter[field]=``)."""

import django_filters

from media.models import MediaAsset


class MediaAssetFilter(django_filters.FilterSet):
    visibility = django_filters.ChoiceFilter(choices=MediaAsset.Visibility.choices)
    kind = django_filters.ChoiceFilter(choices=MediaAsset.Kind.choices)
    folder = django_filters.CharFilter(field_name="folder", help_text="Exact folder.")
    mime_type = django_filters.CharFilter(field_name="mime_type")
    created_from = django_filters.IsoDateTimeFilter(field_name="created_at", lookup_expr="gte")
    created_to = django_filters.IsoDateTimeFilter(field_name="created_at", lookup_expr="lte")

    class Meta:
        model = MediaAsset
        fields: list[str] = []
