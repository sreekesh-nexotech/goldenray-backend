"""Publish a PackRelease locally, so quotations can be created in development.

A quotation is priced from the published PackRelease, not from the BOM
calculator's live market rates: ``quotations.services.create`` calls
``current_pack_release()`` first and refuses the whole request when nothing is
published. A fresh database has none, so the quotation half of the Studio
cannot be exercised at all until this chain exists:

    ACTIVE MarketRateSet -> PriceRelease -> approved ConfigVersion -> PackRelease

Each step is a real service call, not a fixture: a release assembled by hand
would be one the publish report never checked, which is exactly the state this
command exists to avoid producing.

Development only, and it says so: ``publish`` stamps the release with the user
who published it, and the steps it performs are the ones a Project Head signs
off in production.
"""

import json
from pathlib import Path

from django.conf import settings
from django.core.management.base import BaseCommand, CommandError
from django.utils import timezone

CATALOG = Path(settings.BASE_DIR) / "deploy" / "bom" / "catalog.json"


class Command(BaseCommand):
    help = "Seed and publish a PackRelease (development), so quotations can be created."

    def add_arguments(self, parser):
        parser.add_argument("--catalog", default=str(CATALOG), help="catalog.json to build the pack configuration from.")
        parser.add_argument("--force", action="store_true", help="Publish another release even if one is already published.")

    def handle(self, *args, **options):
        # This publishes prices and activates a rate set. Both are decisions a
        # Project Head signs off in production, so the command refuses to run
        # anywhere that is not plainly a development environment.
        if not settings.DEBUG and not options["force"]:
            raise CommandError("DEBUG is off: this looks like a real environment. Publish through the Studio, or pass --force if you are certain.")

        from accounts.models import User
        from packs.models import ConfigStatus, ConfigVersion, PackRelease, ReleaseStatus
        from packs.services import releases as pack_releases
        from packs.services import versions as pack_versions
        from pricing.models import MarketRateSet, MarketRateSetStatus, PriceRelease
        from pricing.services import market_rates
        from pricing.services import releases as price_releases

        published = PackRelease.objects.filter(status=ReleaseStatus.PUBLISHED).first()
        if published and not options["force"]:
            self.stdout.write(self.style.SUCCESS(f"PackRelease #{published.number} is already published ({published.packs.count()} packs). Nothing to do."))
            return

        # Attribution only -- `publish` stamps who published the release. Staff first,
        # since that is who would do this in production.
        user = User.objects.filter(is_staff=True, is_active=True).order_by("id").first() or User.objects.order_by("id").first()
        if user is None:
            raise CommandError("No user exists to attribute the release to. Run `bootstrap_admin` first.")
        self.stdout.write(f"Publishing as {user}.")

        # 1. A market rate set has to be ACTIVE: the PriceRelease reads its cells,
        #    and rates sitting in DRAFT are priced as if they did not exist.
        rate_set = market_rates.active_set()
        if rate_set is None:
            candidate = (
                MarketRateSet.objects.filter(status=MarketRateSetStatus.DRAFT, deleted_at__isnull=True)
                .order_by("-created_at")
                .first()
            )
            if candidate is None:
                raise CommandError("No market rate set to activate. Import prices first (`import_flarize --only flarize.catalog_pricing`).")
            rate_set = market_rates.activate_set(candidate, user=user, note="Seeded for local development.")
            # Worth stating plainly: this is a real pricing change, not scaffolding.
            # Where a market rate exists it *is* the selling price, so activating a
            # set moves what every quote on this database reads.
            self.stdout.write(self.style.WARNING(f"  Activated market rate set {rate_set.uid} (was DRAFT) — this changes what quotes are priced at."))
        else:
            self.stdout.write(f"  Market rate set {rate_set.uid} is already ACTIVE.")

        # 1b. The four GST split keys. They are registered `required_for_release`
        #     but nothing seeds them, so a PriceRelease build blocks on
        #     GST_CONFIG_MISSING until they exist. The values are the audited
        #     70/30 composite the registry documents: 0.70 x 5% + 0.30 x 18% = 8.9%.
        from pricing.services import cost_config

        gst_defaults = {"gst_goods_share": "0.70", "gst_services_share": "0.30", "gst_goods_rate": "0.05", "gst_services_rate": "0.18"}
        have = cost_config.current_values()
        missing = [{"key": key, "value": value} for key, value in gst_defaults.items() if key not in have]
        if missing:
            cost_config.put_values(user=user, entries=missing, note="Seeded for local development (70/30 composite).")
            self.stdout.write(self.style.WARNING(f"  Set GST split keys: {', '.join(entry['key'] for entry in missing)}."))

        # 2. A published PriceRelease pins those rates, the cost config and the
        #    installation matrix, so an issued quotation never re-prices itself.
        price_release = PriceRelease.objects.filter(status="PUBLISHED").first()
        if price_release is None:
            price_release = price_releases.publish(user=user, note="Seeded for local development.")
            self.stdout.write(self.style.WARNING(f"  Published PriceRelease #{price_release.number}."))
        else:
            self.stdout.write(f"  PriceRelease #{price_release.number} is already published.")

        # 3. An approved pack configuration. `seed_from_catalog` turns the BOM
        #    tool's catalog into the ten sections the schema requires, adding the
        #    three it has no concept of (gst, pricing, futureUpgrade).
        version = pack_versions.current_version()
        if version is None:
            from engines.pack_config import seed_from_catalog

            path = Path(options["catalog"])
            if not path.exists():
                raise CommandError(f"No catalog at {path}.")
            seeded = seed_from_catalog(json.loads(path.read_text()), at=timezone.now().isoformat())
            draft = pack_versions.create_draft(user=user, data={"config": seeded["approved"]["config"], "note": "Seeded for local development."})
            version = pack_versions.approve(draft, user=user, note="Seeded for local development.", direct=True)
            self.stdout.write(self.style.WARNING(f"  Approved pack config v{version.number}."))
        else:
            self.stdout.write(f"  Pack config v{version.number} is already {version.status}.")

        # 4. The release itself. `publish` builds every pack, runs the engineering
        #    checker over them and refuses on a blocking report -- so a release
        #    that comes out of this command is one the checks passed.
        release = pack_releases.publish(user=user, note="Seeded for local development.")
        packs = release.packs.count()
        self.stdout.write(self.style.SUCCESS(f"Published PackRelease #{release.number} with {packs} packs."))
        if not packs:
            self.stdout.write(self.style.ERROR("  No packs were built — quotations will still refuse with `no_approved_package`."))
        unused = ConfigVersion.objects.filter(status=ConfigStatus.DRAFT).count()
        if unused:
            self.stdout.write(f"  ({unused} draft config version(s) left open, which is normal.)")
