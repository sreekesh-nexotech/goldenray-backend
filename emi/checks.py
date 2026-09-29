"""System checks of the EMI price source (``settings.EMI_PRICE_SOURCE``, DV-76).

* ``emi.E001`` — the setting is neither ``MANUAL`` nor ``PACK_RELEASE``;
* ``emi.W001`` — ``PACK_RELEASE`` is configured but no provider was registered (``emi.services.price_sources``): the
  calculator would answer 503 for every size. ``manage.py check --fail-level WARNING`` (CI, release) refuses it.
"""

from __future__ import annotations

from django.core.checks import Error, Warning, register


@register()
def check_price_source(app_configs=None, **kwargs):
    from emi.services import price_sources

    value = price_sources.source()
    if value not in price_sources.SOURCES:
        return [Error(f"EMI_PRICE_SOURCE is {value!r}; expected one of {', '.join(price_sources.SOURCES)}.", id="emi.E001")]
    if value == price_sources.PACK_RELEASE and not price_sources.has_provider():
        return [
            Warning("EMI_PRICE_SOURCE is PACK_RELEASE but no pack-release price provider is registered.", hint="Register one with emi.services.price_sources.register in the packs app.", id="emi.W001")
        ]
    return []
