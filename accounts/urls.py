"""accounts owns the staff paths ``auth/``, ``users/`` and ``roles/`` (PLAN §3.4). Every path ends with a slash."""

from django.urls import path
from rest_framework.routers import SimpleRouter

from accounts.views.auth import (
    LoginView,
    LogoutView,
    MeView,
    PasswordChangeView,
    PasswordResetRequestView,
    PasswordResetView,
    RefreshView,
    SessionDetailView,
    SessionListView,
)
from accounts.views.roles import RoleViewSet
from accounts.views.users import UserViewSet

router = SimpleRouter(trailing_slash=True)
router.register("users", UserViewSet, basename="users")
router.register("roles", RoleViewSet, basename="roles")

staff_urlpatterns = [
    path("auth/login/", LoginView.as_view(), name="auth-login"),
    path("auth/refresh/", RefreshView.as_view(), name="auth-refresh"),
    path("auth/logout/", LogoutView.as_view(), name="auth-logout"),
    path("auth/me/", MeView.as_view(), name="auth-me"),
    path("auth/password/change/", PasswordChangeView.as_view(), name="auth-password-change"),
    path("auth/password/reset-request/", PasswordResetRequestView.as_view(), name="auth-password-reset-request"),
    path("auth/password/reset/", PasswordResetView.as_view(), name="auth-password-reset"),
    path("auth/sessions/", SessionListView.as_view(), name="auth-sessions"),
    path("auth/sessions/<uuid:uid>/", SessionDetailView.as_view(), name="auth-session-detail"),
    *router.urls,
]
public_urlpatterns: list = []
agent_urlpatterns: list = []
customer_urlpatterns: list = []
