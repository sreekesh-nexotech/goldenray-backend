"""Register the website's real routes as maintainable pages and create the slots their components read.

    python manage.py seed_pages

Additive and idempotent (see ``sitepages.services.registry``): missing pages and slots are created, nothing existing
is modified or deleted. Run after ``migrate`` on every release.
"""

from django.core.management.base import BaseCommand

from sitepages.services.registry import slug_for_route, sync_registry


class Command(BaseCommand):
    help = "Register the public website's routes as maintainable pages (idempotent)."

    def handle(self, *args, **options):
        result = sync_registry(user=None)
        self.stdout.write(self.style.SUCCESS(f"{result['pages_created']} page(s) and {result['slots_created']} slot(s) created."))
        for route in result["conflicts"]:
            self.stdout.write(self.style.WARNING(f"  ! {route} not registered: another page already uses the slug '{slug_for_route(route)}'."))
