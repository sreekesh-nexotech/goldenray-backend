"""``manage.py seed_roles`` — create the twelve system roles of PLAN §3.2 (get-or-create by slug).

Existing roles are never modified: grants edited by an Admin survive every re-run.
"""

from django.core.management.base import BaseCommand

from accounts.services.seeds import seed_roles


class Command(BaseCommand):
    help = "Create the missing system roles (PLAN §3.2). Existing roles are left untouched."

    def handle(self, *args, **options):
        results = seed_roles()
        for role, created in results:
            if created:
                self.stdout.write(self.style.SUCCESS(f"created  {role.slug}"))
            else:
                self.stdout.write(f"exists   {role.slug}")
        created_count = sum(1 for _, created in results if created)
        self.stdout.write(f"{created_count} created, {len(results) - created_count} already present")
