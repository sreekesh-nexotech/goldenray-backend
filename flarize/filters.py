"""Default django-filter backend accepting both ``?field=value`` and ``?filter[field]=value`` (PLAN §3.1)."""

from __future__ import annotations

import re

from django.http import QueryDict
from django_filters.rest_framework import DjangoFilterBackend

_BRACKET_RE = re.compile(r"^filter\[(?P<name>[A-Za-z0-9_]+)\]$")


def normalise_filter_params(query_params: QueryDict) -> QueryDict:
    """Copy of ``query_params`` where ``filter[x]`` is also available as ``x`` (explicit ``x`` wins)."""
    data = query_params.copy()
    for key in list(query_params.keys()):
        match = _BRACKET_RE.match(key)
        if match and match.group("name") not in query_params:
            data.setlist(match.group("name"), query_params.getlist(key))
    return data


class FilterBackend(DjangoFilterBackend):
    def get_filterset_kwargs(self, request, queryset, view):
        kwargs = super().get_filterset_kwargs(request, queryset, view)
        kwargs["data"] = normalise_filter_params(request.query_params)
        return kwargs
