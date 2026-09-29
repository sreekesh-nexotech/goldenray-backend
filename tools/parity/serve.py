#!/usr/bin/env python3
"""Run the NEW side of a parity run: this checkout's API on a database loaded from the UAT dumps.

    DB_NAME=<target db> python tools/parity/serve.py 127.0.0.1:18150

Before serving it puts the target in the state the legacy UAT servers are in (rehearsal mode, never used in
production — docs/decisions/legacy-shim.md, "Parity environment"):

* ``LEGACY_API_SHIM`` on (feature-flag default for this process only);
* the imported market-rate set ACTIVE (the importer leaves it DRAFT for the business to review; the legacy BOM
  calculator used its rates directly) — idempotent;
* website product prices from the imported current LIST rows standing in for PriceRelease #1 (the same rehearsal
  provider as ``verify_migration --list-prices-as-release``), because no release can be published from the website
  sources alone (docs/migration/rehearsal-website.md, finding 1);
* throttles lifted (the harness sends a few hundred requests from one address) and a private Redis database.
"""

from __future__ import annotations

import os
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))
os.environ.setdefault("DJANGO_SETTINGS_MODULE", "flarize.settings.dev")
os.environ.setdefault("REDIS_URL", "redis://localhost:6379/13")


def configure() -> None:
    from django.conf import settings

    settings.FEATURE_FLAG_DEFAULTS = {**settings.FEATURE_FLAG_DEFAULTS, "LEGACY_API_SHIM": True}
    rates = {scope: "100000/min" for scope in settings.REST_FRAMEWORK["DEFAULT_THROTTLE_RATES"]}
    settings.REST_FRAMEWORK = {**settings.REST_FRAMEWORK, "DEFAULT_THROTTLE_RATES": rates}


def prepare() -> None:
    from catalog.services import pricing_hooks
    from flarize.cache_utils import bump
    from pricing.models import MarketRateSet
    from pricing.services.market_rates import activate_set, active_set
    from pricing.services.prices import current_row

    if active_set() is None:
        imported = MarketRateSet.objects.filter(name__startswith="Imported").order_by("-created_at").first()
        if imported is not None:
            activate_set(imported, user=None, note="parity rehearsal: the legacy calculator used these rates directly")

    def provider(components):
        prices = {}
        for component in components:
            row = current_row(component, "LIST")
            if row is not None and row.amount > 0:
                prices[component.pk] = pricing_hooks.PriceInfo(min_amount=row.amount, max_amount=row.amount)
        return prices

    pricing_hooks.register(provider)
    bump("pricing")


def main(argv: list[str]) -> None:
    import django
    from django.core.management import call_command

    django.setup()
    configure()
    prepare()
    call_command("runserver", argv[0] if argv else "127.0.0.1:18150", "--noreload", "--nothreading")


if __name__ == "__main__":
    main(sys.argv[1:])
