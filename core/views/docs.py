"""The OpenAPI schema and Swagger UI views (``/api/schema/<version>/``, ``/api/docs/``).

drf-spectacular's views negotiate YAML/OpenAPI or HTML renderers, so their errors (405, 403, 406 …) would be rendered
as YAML or a bare HTML page. These subclasses render every error response in the JSON envelope instead. Access is
``SPECTACULAR_SETTINGS["SERVE_PERMISSIONS"]`` (``core.permissions.ApiDocsAccess``).
"""

from __future__ import annotations

from drf_spectacular.views import SpectacularAPIView, SpectacularSwaggerView
from rest_framework.renderers import JSONRenderer
from rest_framework.response import Response


class JsonErrorsMixin:
    def finalize_response(self, request, response, *args, **kwargs):
        if isinstance(response, Response) and response.status_code >= 400:
            request.accepted_renderer = JSONRenderer()
            request.accepted_media_type = JSONRenderer.media_type
        return super().finalize_response(request, response, *args, **kwargs)


class SchemaView(JsonErrorsMixin, SpectacularAPIView):
    pass


class SwaggerView(JsonErrorsMixin, SpectacularSwaggerView):
    pass
