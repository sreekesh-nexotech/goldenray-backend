"""``manage.py ensure_audit_partitions [--months N] [--app-role ROLE]``

Creates the monthly ``audit_log`` partitions for the current (UTC) month and the next ``N - 1`` months, moving any
rows that landed in ``audit_log_default`` meanwhile, and closes the new partitions to the application role. Run it
as the **owner** role on every deploy and monthly from cron (docs/ops/audit-log.md). Idempotent.
"""

from django.conf import settings
from django.core.management.base import BaseCommand, CommandError

from audit.services.partitions import PrivilegeError, apply_append_only_privileges, check_app_role, ensure_partitions, table_owner


class Command(BaseCommand):
    help = "Create the upcoming monthly audit_log partitions (idempotent) and keep the table append-only for the app role."

    def add_arguments(self, parser):
        parser.add_argument("--months", type=int, default=3, help="Partitions to guarantee, starting with the current month (default 3).")
        parser.add_argument("--app-role", default=None, help="Application role to keep append-only (default: DB_APP_ROLE).")

    def handle(self, *args, months: int, app_role: str | None, **options):
        if months < 1 or months > 36:
            raise CommandError("--months must be between 1 and 36.")
        role = (app_role if app_role is not None else getattr(settings, "DB_APP_ROLE", "")).strip() or None
        if role and role == table_owner():
            self.stdout.write(self.style.WARNING(f"{role!r} owns audit_log (single-role setup): privileges left unchanged"))
            role = None
        if role:
            try:
                check_app_role(role)
            except PrivilegeError as exc:
                raise CommandError(str(exc)) from exc
        results = ensure_partitions(months, app_role=role)
        for result in results:
            if result.created:
                moved = f" ({result.moved_rows} row(s) moved from the default partition)" if result.moved_rows else ""
                self.stdout.write(self.style.SUCCESS(f"created {result.name}{moved}"))
            else:
                self.stdout.write(f"exists  {result.name}")
        if role:
            apply_append_only_privileges(role)
            self.stdout.write(self.style.SUCCESS(f"audit_log is append-only for role {role!r}"))
