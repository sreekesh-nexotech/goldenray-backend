"""Root URLconf. Strictly versioned; nothing else may be routed (see core/tests/test_url_versioning.py).

* ``/api/<version>/``            staff (JWT)            ← every app's ``staff_urlpatterns``
* ``/api/public/<version>/``     website (AllowAny)     ← ``public_urlpatterns``
* ``/api/agent/<version>/``      office agents          ← ``agent_urlpatterns``
* ``/api/customer/<version>/``   customer signed links  ← ``customer_urlpatterns``
* ``/iclock/<device_token>/``    terminals (plain Django views, flag ADMS_RECEIVER) ← ``devices.urls.iclock_urlpatterns``
* ``/legacy/``                   old contracts (flag LEGACY_API_SHIM) ← ``legacy.urls.legacy_urlpatterns``
* ``/api/schema/<version>/``, ``/api/docs/``, ``/healthz``
"""

from django.conf import settings
from django.urls import include, path, re_path, reverse_lazy
from drf_spectacular.views import SpectacularAPIView, SpectacularSwaggerView

from core.views.health import healthz
from flarize.versioning import allowed_versions, collect, mount_surfaces, version_group

urlpatterns = [
    *mount_surfaces(settings.LOCAL_APPS),
    path("iclock/<str:device_token>/", include((collect("iclock_urlpatterns", ["devices"]), "iclock"))),
    path("legacy/", include((collect("legacy_urlpatterns", ["legacy"]), "legacy"))),
    re_path(rf"^api/schema/{version_group()}/$", SpectacularAPIView.as_view(), name="api-schema"),
    # The Swagger UI shell is the only unversioned DRF view: under URLPathVersioning any DRF view without a version
    # kwarg answers 404, so it opts out explicitly (legacy adapters do the same under /legacy/).
    path("api/docs/", SpectacularSwaggerView.as_view(versioning_class=None, url=reverse_lazy("api-schema", kwargs={"version": allowed_versions()[-1]})), name="api-docs"),
    path("healthz", healthz, name="healthz"),
]

handler400 = "flarize.exceptions.bad_request_view"
handler403 = "flarize.exceptions.permission_denied_view"
handler404 = "flarize.exceptions.not_found_view"
handler500 = "flarize.exceptions.server_error_view"
