"""``manage.py ensure_inventory_append_only [--app-role ROLE]``

Keeps ``inventory_movement`` append-only for the application role: ``SELECT, INSERT`` (and the id sequence) granted,
``UPDATE, DELETE, TRUNCATE`` revoked, ``SELECT`` on the ``inventory_balance`` view. Run it as the **owner** role on
every deploy (``deploy/release.sh``), right after ``migrate``. Idempotent; a single-role setup (the app role owns the
table) is left unchanged.
"""

from django.conf import settings
from django.core.management.base import BaseCommand, CommandError

from inventory.services.privileges import PrivilegeError, apply_append_only_privileges, table_owner


class Command(BaseCommand):
    help = "Keep inventory_movement append-only for the application role (REVOKE UPDATE, DELETE, TRUNCATE). Idempotent."

    def add_arguments(self, parser):
        parser.add_argument("--app-role", default=None, help="Application role to keep append-only (default: DB_APP_ROLE).")

    def handle(self, *args, app_role: str | None, **options):
        role = (app_role if app_role is not None else getattr(settings, "DB_APP_ROLE", "")).strip() or None
        if role is None:
            self.stdout.write("DB_APP_ROLE is not set (single-role setup): privileges left unchanged")
            return
        if role == table_owner():
            self.stdout.write(self.style.WARNING(f"{role!r} owns inventory_movement (single-role setup): privileges left unchanged"))
            return
        try:
            apply_append_only_privileges(role)
        except PrivilegeError as exc:
            raise CommandError(str(exc)) from exc
        self.stdout.write(self.style.SUCCESS(f"inventory_movement is append-only for role {role!r}"))
