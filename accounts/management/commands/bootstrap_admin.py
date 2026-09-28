"""``manage.py bootstrap_admin --email admin@example.com [--first-name A] [--last-name B]``

Seeds the system roles, creates a Super Admin **without a password** (``must_reset_password``) and prints a
one-time set-password link valid for ``ACCOUNTS_BOOTSTRAP_TTL``. There is no default password anywhere; if the link
expires, use "Forgot password" on the sign-in page. Also the recovery path when every Super Admin is locked out.
"""

from django.conf import settings
from django.core.management.base import BaseCommand, CommandError

from accounts.services.users import bootstrap_super_admin
from core.errors import DomainError


class Command(BaseCommand):
    help = "Create a Super Admin without a password and print a one-time set-password link."

    def add_arguments(self, parser):
        parser.add_argument("--email", required=True)
        parser.add_argument("--first-name", default="")
        parser.add_argument("--last-name", default="")

    def handle(self, *args, email: str, first_name: str, last_name: str, **options):
        try:
            user, reset = bootstrap_super_admin(email=email, first_name=first_name, last_name=last_name)
        except DomainError as exc:
            raise CommandError(f"{exc.code}: {exc.message}") from exc
        hours = int(settings.ACCOUNTS_BOOTSTRAP_TTL.total_seconds() // 3600)
        self.stdout.write(self.style.SUCCESS(f"Super Admin {user.email} created (uid {user.uid})."))
        self.stdout.write(f"Set the password within {hours} hours (single use): {reset.link}")
