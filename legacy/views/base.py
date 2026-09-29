"""Base class of every ``/legacy/…`` adapter (PLAN §6, DV-5).

* Behind the ``LEGACY_API_SHIM`` flag: while it is off every legacy URL answers the platform's plain 404, exactly like
  an unrouted path (a disabled surface is indistinguishable from an absent one).
* ``versioning_class = None`` (legacy paths carry no version), anonymous, throttled like the equivalent public endpoint
  (``public_read`` for reads and the calculators, ``public_write`` for forms; views override ``throttle_scope``).
* Errors keep the OLD bodies: DRF's own exceptions (405 ``{"detail": "Method \\"PUT\\" not allowed."}``, throttling,
  JSON parse errors) are rendered by DRF's default handler, as the legacy DRF servers did; a ``DomainError`` raised by
  a platform service is rendered by the view's :meth:`LegacyView.legacy_error` (``{"error": message}`` by default).
  Anything unexpected goes to the platform handler (logged, recorded, 500 envelope).
* Not in the OpenAPI schema (``schema = None``): the old contracts are documented in docs/decisions/legacy-shim.md.
"""

from __future__ import annotations

from django.http import Http404, HttpResponsePermanentRedirect
from rest_framework import exceptions as drf_exceptions
from rest_framework import permissions
from rest_framework.renderers import JSONRenderer
from rest_framework.response import Response
from rest_framework.views import APIView
from rest_framework.views import exception_handler as drf_exception_handler

from core.errors import DomainError
from core.flags import flag_enabled
from core.views import PublicViewMixin
from flarize.exceptions import exception_handler as platform_exception_handler
from flarize.exceptions import not_found_view
from flarize.parsers import JSONParser

FLAG = "LEGACY_API_SHIM"
LEGACY_PREFIX = "/legacy"


def legacy_exception_handler(exc, context):
    view = context.get("view")
    if isinstance(exc, DomainError) and hasattr(view, "legacy_error"):
        return view.legacy_error(exc)
    if isinstance(exc, (Http404, drf_exceptions.APIException)):
        return drf_exception_handler(exc, context)
    return platform_exception_handler(exc, context)


class LegacyView(PublicViewMixin, APIView):
    versioning_class = None
    authentication_classes: list = []
    permission_classes = [permissions.AllowAny]
    renderer_classes = [JSONRenderer]
    parser_classes = [JSONParser]
    schema = None

    def dispatch(self, request, *args, **kwargs):
        if not flag_enabled(FLAG):
            return not_found_view(request)
        return super().dispatch(request, *args, **kwargs)

    def get_exception_handler(self):
        return legacy_exception_handler

    def legacy_error(self, exc: DomainError) -> Response:
        return Response({"error": exc.message}, status=exc.status)


def old_path(request) -> str:
    """The URL the website called (nginx rewrote ``/<old path>`` to ``/legacy/<old path>``)."""
    return request.path.removeprefix(LEGACY_PREFIX)


class AppendSlashView(LegacyView):
    """The legacy Django ``APPEND_SLASH``: a slash-less old URL answers 301 to the old URL with the slash (any method —
    the legacy answered a slash-less POST with a DEBUG-only 500, see DV)."""

    http_method_names = ["get", "post", "put", "patch", "delete", "head", "options"]

    def _redirect(self, request, *args, **kwargs):
        query = request.META.get("QUERY_STRING", "")
        return HttpResponsePermanentRedirect(old_path(request) + "/" + (("?" + query) if query else ""))

    get = post = put = patch = delete = head = options = _redirect
