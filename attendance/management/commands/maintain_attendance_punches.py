"""``manage.py maintain_attendance_punches [--months N] [--app-role ROLE]``

Run as the **owner** role on every deploy (``deploy/release.sh``), right after ``migrate``:

* creates the ``attendance_raw_punch`` partitions of the current month and the next ``N - 1`` (default 3), moving any
  rows that landed in the DEFAULT partition into their month;
* keeps the table append-only for the application role (``DB_APP_ROLE`` or ``--app-role``): ``SELECT, INSERT``
  granted, ``UPDATE, DELETE, TRUNCATE`` revoked, partitions closed to direct access. A single-role setup (the app role
  owns the table) is left unchanged.

Idempotent.
"""

from django.conf import settings
from django.core.management.base import BaseCommand, CommandError

from attendance.services.partitions import PrivilegeError, apply_append_only_privileges, ensure_partitions, table_owner


class Command(BaseCommand):
    help = "Create the next months' attendance_raw_punch partitions and keep the table append-only for the app role. Idempotent."

    def add_arguments(self, parser):
        parser.add_argument("--months", type=int, default=3, help="Partitions to keep ready, the current month included (default 3).")
        parser.add_argument("--app-role", default=None, help="Application role to keep append-only (default: DB_APP_ROLE).")

    def handle(self, *args, months: int, app_role: str | None, **options):
        if months < 1:
            raise CommandError("--months must be at least 1.")
        role = (app_role if app_role is not None else getattr(settings, "DB_APP_ROLE", "")).strip() or None
        if role is not None and role == table_owner():
            self.stdout.write(self.style.WARNING(f"{role!r} owns attendance_raw_punch (single-role setup): privileges left unchanged"))
            role = None
        created = ensure_partitions(months, app_role=role)
        for item in created:
            self.stdout.write(f"created {item['name']} (moved {item['moved_rows']} row(s) out of the default partition)")
        if role is None:
            self.stdout.write("DB_APP_ROLE is not set or owns the table: privileges left unchanged")
            return
        try:
            closed = apply_append_only_privileges(role)
        except PrivilegeError as exc:
            raise CommandError(str(exc)) from exc
        self.stdout.write(self.style.SUCCESS(f"attendance_raw_punch is append-only for role {role!r} ({len(closed)} partition(s) closed)"))
