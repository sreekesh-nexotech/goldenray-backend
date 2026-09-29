"""The public EMI calculator (``calculators/emi/config/``, ``calculators/emi/``, ``calculators/emi/quotation/``).

The configuration (settings, the active sizes of the price source, the active rules in force today) is loaded
once into an :class:`engines.emi.EmiConfig` snapshot and kept in the cache under the ``emi:config`` namespace
(+ the price source's namespaces, + the day, because rules have effective dates): a calculation costs no query while
nothing changed, and any staff write is visible at once. The arithmetic is :mod:`engines.emi`, a faithful port of the
legacy ``goldenray/utils/emi.py``; engine errors become ``DomainError`` with the legacy message.
"""

from __future__ import annotations

import datetime as dt
import logging
from decimal import Decimal

from django.core.cache import cache
from django.db.models import Q
from django.utils import timezone

from core.errors import DomainError
from emi.models import Bank, InterestRateRule, SubsidyRule
from emi.services import price_sources
from emi.services.rows import CACHE_NAMESPACE
from emi.services.settings import current_settings
from engines import emi as engine
from flarize.cache_utils import build_key, get_versions

logger = logging.getLogger("flarize.emi")

SNAPSHOT_TTL_SECONDS = 300
PERCENT = Decimal("0.01")


def percent(fraction: Decimal | None) -> Decimal | None:
    """A stored fraction as the legacy percentage (``0.0575`` → ``5.75``)."""
    return None if fraction is None else (Decimal(fraction) * 100).quantize(PERCENT)


def cache_namespaces() -> list[str]:
    return [CACHE_NAMESPACE, *price_sources.cache_namespaces()]


def engine_settings(row) -> engine.EmiSettings:
    return engine.EmiSettings(
        tenure_min_years=row.tenure_min_years,
        tenure_max_years=row.tenure_max_years,
        tenure_default_years=row.tenure_default_years,
        daily_saving_divisor=row.daily_saving_divisor,
        price_step=row.price_step,
        down_payment_min_percent=percent(row.down_payment_min_pct),
        down_payment_max_percent=percent(row.down_payment_max_pct),
        down_payment_step_percent=percent(row.down_payment_step_pct),
        down_payment_quick_adds=tuple(row.down_payment_quick_adds or ()),
        rate_max=percent(row.rate_max),
        default_interest_rate=percent(row.default_annual_rate),
        panel_life_years=row.panel_life_years,
    )


def _in_force(on: dt.date) -> Q:
    return (Q(effective_from__isnull=True) | Q(effective_from__lte=on)) & (Q(effective_to__isnull=True) | Q(effective_to__gte=on))


def load_config(on: dt.date) -> engine.EmiConfig:
    sizes = tuple(
        engine.SystemSize(
            uid=option.uid,
            label=option.label,
            capacity_kw=option.capacity_kw,
            price_per_kw=option.price_per_kw,
            price_min=option.price_min,
            price_max=option.price_max,
            monthly_bill_reference=option.monthly_bill_reference,
            sort_order=option.sort_order,
            position=option.position,
            system_cost=option.system_cost,
        )
        for option in price_sources.active_sizes()
    )
    subsidy_rules = tuple(
        engine.SubsidyRule(
            uid=str(rule.uid),
            label=rule.label,
            min_kw=rule.kw_from,
            max_kw=rule.kw_to,
            amount=rule.amount,
            priority=rule.priority,
            amount_per_kw=rule.amount_per_kw,
            cap_amount=rule.cap_amount,
            position=rule.pk,
        )
        for rule in SubsidyRule.objects.filter(_in_force(on), is_active=True)
    )
    interest_rules = tuple(
        engine.InterestRule(
            uid=str(rule.uid),
            label=rule.label,
            rate=percent(rule.annual_rate),
            min_rate=percent(rule.min_annual_rate),
            is_locked=rule.is_locked,
            priority=rule.priority,
            min_kw=rule.min_kw,
            max_kw=rule.max_kw,
            min_cost=rule.min_system_cost,
            max_cost=rule.max_system_cost,
            min_loan=rule.min_amount,
            max_loan=rule.max_amount,
            position=rule.pk,
        )
        for rule in InterestRateRule.objects.filter(_in_force(on), is_active=True)
    )
    return engine.EmiConfig(settings=engine_settings(current_settings()), sizes=sizes, subsidy_rules=subsidy_rules, interest_rules=interest_rules)


def config_snapshot(on: dt.date | None = None) -> engine.EmiConfig:
    """The calculator's configuration, from the cache while no ``emi:config`` write happened (fail-soft)."""
    on = on or timezone.localdate()
    key = build_key("emi:snapshot", price_sources.source(), on.isoformat(), versions=get_versions(cache_namespaces()))
    try:
        snapshot = cache.get(key)
    except Exception:  # noqa: BLE001 - a cache outage must not break the calculator
        logger.warning("emi snapshot cache read failed", exc_info=True)
        snapshot = None
    if snapshot is None:
        snapshot = load_config(on)
        try:
            cache.set(key, snapshot, SNAPSHOT_TTL_SECONDS)
        except Exception:  # noqa: BLE001
            logger.warning("emi snapshot cache write failed", exc_info=True)
    return snapshot


def public_config() -> dict:
    """``GET calculators/emi/config/``: the settings, the active size tiles of the price source and the active banks."""
    return {
        "settings": current_settings(),
        "system_sizes": price_sources.active_sizes(),
        "banks": list(Bank.objects.filter(is_active=True).order_by("sort_order", "annual_rate", "id")),
    }


def _domain_error(exc: engine.EmiError) -> DomainError:
    if exc.code == "invalid_input":
        logger.info("emi calculator input refused", extra={"detail": exc.detail})
    return DomainError(exc.code, exc.message, status=exc.status)


def calculate(body) -> dict:
    """``POST calculators/emi/``: the breakdown for a size (``size_uid``) or capacity and the customer's adjustments."""
    try:
        return engine.calculate(body, config_snapshot())
    except engine.EmiError as exc:
        raise _domain_error(exc) from None


def quotation(body) -> dict:
    """``POST calculators/emi/quotation/``: the calculator's policy applied to quotation package prices."""
    try:
        return engine.quotation(body, config_snapshot())
    except engine.EmiError as exc:
        raise _domain_error(exc) from None
