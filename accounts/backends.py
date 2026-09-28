"""Password authentication backend for staff users.

E-mail uniqueness is a *partial* unique constraint (live rows only, PLAN §2.1) so a soft-deleted user does not
reserve their address forever; Django accepts that only with a custom backend (``auth.W004`` is silenced in settings
for this reason). Django's own permission API is disabled here: authorization is the closed registry in
``accounts.registry`` resolved by ``accounts.services.authz`` — nothing may grant access through ``has_perm``.
"""

from django.contrib.auth import get_user_model
from django.contrib.auth.backends import ModelBackend


class EmailBackend(ModelBackend):
    def user_can_authenticate(self, user) -> bool:
        return bool(getattr(user, "is_active", False)) and getattr(user, "deleted_at", None) is None

    def get_user_permissions(self, user_obj, obj=None):
        return set()

    def get_group_permissions(self, user_obj, obj=None):
        return set()

    def get_all_permissions(self, user_obj, obj=None):
        return set()

    def has_perm(self, user_obj, perm, obj=None) -> bool:
        return False

    def has_module_perms(self, user_obj, app_label) -> bool:
        return False

    def with_perm(self, perm, is_active=True, include_superusers=True, obj=None):
        return get_user_model()._default_manager.none()
