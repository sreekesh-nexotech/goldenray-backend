"""Service-delegating DRF mixins. Use these instead of ``rest_framework.mixins`` in every viewset.

DRF's own ``perform_create``/``perform_update`` call ``serializer.save()`` (an ORM write from the view) and its
``perform_destroy`` hard-deletes. Here all three delegate to the functions in the view's ``services`` map:

* ``services["create"](user=..., data=validated_data)`` → instance
* ``services["update"](instance, user=..., data=validated_data, expected_version=...)`` → instance
* ``services["destroy"](instance, user=..., expected_version=...)``

The ``core.E005`` system check rejects any routed view that still resolves to DRF's default ``perform_*``.
"""

from __future__ import annotations

from django.core.exceptions import ImproperlyConfigured
from rest_framework import mixins

from core.services.versioning import parse_expected_version


class ServiceWritesMixin:
    services: dict = {}

    def get_service(self, name: str):
        service = (self.services or {}).get(name)
        if service is None:
            raise ImproperlyConfigured(f"{self.__class__.__name__}.services has no {name!r} entry; writes must go through a service.")
        return service

    def expected_version(self, data=None):
        """``expected_version`` from ``data`` (validated body) or the request body, else from the query string."""
        source = data if data is not None else self.request.data
        if hasattr(source, "get") and source.get("expected_version") is not None:
            return parse_expected_version(source.get("expected_version"))
        return parse_expected_version(self.request.query_params.get("expected_version"))

    def perform_create(self, serializer):
        data = dict(serializer.validated_data)
        data.pop("expected_version", None)
        serializer.instance = self.get_service("create")(user=self.request.user, data=data)

    def perform_update(self, serializer):
        data = dict(serializer.validated_data)
        expected = self.expected_version(data)
        data.pop("expected_version", None)
        serializer.instance = self.get_service("update")(serializer.instance, user=self.request.user, data=data, expected_version=expected)

    def perform_destroy(self, instance):
        self.get_service("destroy")(instance, user=self.request.user, expected_version=self.expected_version())


class ListModelMixin(mixins.ListModelMixin):
    pass


class RetrieveModelMixin(mixins.RetrieveModelMixin):
    pass


class CreateModelMixin(ServiceWritesMixin, mixins.CreateModelMixin):
    pass


class UpdateModelMixin(ServiceWritesMixin, mixins.UpdateModelMixin):
    pass


class DestroyModelMixin(ServiceWritesMixin, mixins.DestroyModelMixin):
    pass


DRF_DEFAULT_WRITES = {
    "perform_create": mixins.CreateModelMixin.perform_create,
    "perform_update": mixins.UpdateModelMixin.perform_update,
    "perform_destroy": mixins.DestroyModelMixin.perform_destroy,
}
