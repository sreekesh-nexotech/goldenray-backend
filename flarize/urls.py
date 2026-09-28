"""Root URLconf. Strictly versioned; nothing else may be routed (see core/tests/test_url_versioning.py).

* ``/api/<version>/``            staff (JWT)            ← every app's ``staff_urlpatterns``
* ``/api/public/<version>/``     website (AllowAny)     ← ``public_urlpatterns``
* ``/api/agent/<version>/``      office agents          ← ``agent_urlpatterns``
* ``/api/customer/<version>/``   customer signed links  ← ``customer_urlpatterns``
* ``/iclock/<device_token>/``    terminals (plain Django views, flag ADMS_RECEIVER) ← ``devices.urls.iclock_urlpatterns``
* ``/legacy/``                   old contracts (flag LEGACY_API_SHIM) ← ``legacy.urls.legacy_urlpatterns``
* ``/api/schema/<version>/``, ``/api/docs/``, ``/healthz``
* dev only (``DEBUG`` with the local public media backend): ``PUBLIC_MEDIA_URL`` serves public uploads. Staging and
  prod serve public files from Bunny and private files through nginx after a signed-URL check — never from here.
"""

from django.conf import settings
from django.conf.urls.static import static
from django.urls import include, path, re_path, reverse_lazy

from core.views.docs import SchemaView, SwaggerView
from core.views.health import healthz
from flarize.versioning import allowed_versions, collect, mount_surfaces, version_group

urlpatterns = [
    *mount_surfaces(settings.LOCAL_APPS),
    path("iclock/<str:device_token>/", include((collect("iclock_urlpatterns", ["devices"]), "iclock"))),
    path("legacy/", include((collect("legacy_urlpatterns", ["legacy"]), "legacy"))),
    re_path(rf"^api/schema/{version_group()}/$", SchemaView.as_view(), name="api-schema"),
    # The Swagger UI shell is the only unversioned DRF view: under URLPathVersioning any DRF view without a version
    # kwarg answers 404, so it opts out explicitly (legacy adapters do the same under /legacy/).
    path("api/docs/", SwaggerView.as_view(versioning_class=None, url=reverse_lazy("api-schema", kwargs={"version": allowed_versions()[-1]})), name="api-docs"),
    path("healthz", healthz, name="healthz"),
]


def public_media_debug_patterns() -> list:
    """Serve the local *public* media backend while developing (private files are never served statically)."""
    if not settings.DEBUG or settings.MEDIA_PUBLIC_BACKEND != "local":
        return []
    return static(settings.PUBLIC_MEDIA_URL, document_root=settings.PUBLIC_MEDIA_ROOT)


urlpatterns += public_media_debug_patterns()

handler400 = "flarize.exceptions.bad_request_view"
handler403 = "flarize.exceptions.permission_denied_view"
handler404 = "flarize.exceptions.not_found_view"
handler500 = "flarize.exceptions.server_error_view"
