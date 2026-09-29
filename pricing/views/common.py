"""Helpers shared by the pricing (and procurement) views: OpenAPI error map, the ``internal`` serializer context."""

from __future__ import annotations

from rest_framework.exceptions import MethodNotAllowed

from core.serializers import ErrorSerializer
from pricing.services.common import can_see_internal

TAGS = ["pricing"]
UUID_REGEX = "[0-9a-fA-F-]{36}"
READ_ERRORS = {401: ErrorSerializer, 403: ErrorSerializer, 404: ErrorSerializer}
WRITE_ERRORS = {400: ErrorSerializer, 401: ErrorSerializer, 403: ErrorSerializer, 404: ErrorSerializer, 409: ErrorSerializer}


class InternalContextMixin:
    """Adds ``internal`` (``pricing_internal.view``) to the serializer context so internal fields can be dropped."""

    def get_serializer_context(self):
        context = super().get_serializer_context()
        context["internal"] = can_see_internal(self.request.user)
        return context

    def internal_context(self) -> dict:
        return {"request": self.request, "internal": can_see_internal(self.request.user)}


class PatchOnlyUpdateMixin:
    """For viewsets that allow PUT only on their child collections: PUT on the record itself is 405 (PATCH edits it).

    The viewset maps ``update`` to its edit permission (so a caller who may edit gets 405, not 403) and excludes it from
    the schema.
    """

    def update(self, request, *args, **kwargs):
        if not kwargs.get("partial"):
            raise MethodNotAllowed(request.method)
        return super().update(request, *args, **kwargs)
