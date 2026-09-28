"""URLconf for core tests: the real routes plus test-only views under the versioned surfaces."""

from django.urls import include, path, re_path
from rest_framework.routers import SimpleRouter

from core.tests import support
from flarize.urls import urlpatterns as real_urlpatterns

router = SimpleRouter()
router.register("test-flags", support.FlagRowViewSet, basename="test-flags")
router.register("test-unmapped", support.UnmappedViewSet, basename="test-unmapped")

staff = [
    path("echo/", support.EchoVersionView.as_view()),
    path("raise/<str:kind>/", support.RaiseView.as_view()),
    path("validate/", support.RaiseView.as_view()),
    path("gated/", support.GatedView.as_view()),
    path("staff-method/", support.StaffMethodView.as_view()),
    *router.urls,
]
public = [
    path("echo/", support.PublicEchoView.as_view()),
    path("things/", support.ThingsView.as_view()),
    path("no-namespace/", support.NoNamespaceView.as_view()),
    path("idempotent/", support.IdempotentView.as_view()),
    path("idempotent-required/", support.RequiredIdempotentView.as_view()),
    path("otp-like/", support.OtpLikeView.as_view()),
]
agent = [path("ping/", support.AgentPingView.as_view())]

urlpatterns = [
    re_path(r"^api/(?P<version>v1)/_t/", include(staff)),
    re_path(r"^api/public/(?P<version>v1)/_t/", include(public)),
    re_path(r"^api/agent/(?P<version>v1)/_t/", include(agent)),
    path("iclock/<str:device_token>/_t/", support.plain_gated_view),
    path("iclock/<str:device_token>/_t/boom/", support.plain_boom_view),
    *real_urlpatterns,
]

handler404 = "flarize.exceptions.not_found_view"
handler500 = "flarize.exceptions.server_error_view"
