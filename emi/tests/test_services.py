"""emi services: list writes (every DomainError), settings, price sources, snapshot, system checks."""

import datetime as dt
from decimal import Decimal

import pytest
from django.test import override_settings

from core.errors import Conflict, DomainError, StaleVersion
from emi import checks
from emi.models import Bank, EmiSettings
from emi.services import calculator, price_sources, rows
from emi.services import settings as emi_settings
from emi.tests.factories import BankFactory, EmiSettingsFactory, InterestRateRuleFactory, SubsidyRuleFactory, SystemSizeFactory

pytestmark = pytest.mark.django_db


@pytest.fixture(autouse=True)
def _no_provider():
    price_sources.reset()
    yield
    price_sources.reset()


class TestRows:
    def test_validation_of_every_list(self):
        with pytest.raises(DomainError) as excinfo:
            rows.create_row(rows.INTEREST_RULES, user=None, data={"label": "x", "min_kw": Decimal("5"), "max_kw": Decimal("3"), "annual_rate": Decimal("0.08"), "min_annual_rate": Decimal("0.08")})
        assert excinfo.value.errors == {"max_kw": ["Upper bound must be greater than or equal to the lower bound."]}
        with pytest.raises(DomainError) as excinfo:
            rows.create_row(rows.SUBSIDY_RULES, user=None, data={"label": "x", "amount": Decimal("1"), "effective_from": dt.date(2026, 2, 1), "effective_to": dt.date(2026, 1, 1)})
        assert "effective_to" in excinfo.value.errors
        with pytest.raises(DomainError) as excinfo:
            rows.create_row(rows.BANKS, user=None, data={"name": "B", "abbr": "B", "annual_rate": Decimal("0.08"), "approval_min_days": 9, "approval_max_days": 3})
        assert "approval_max_days" in excinfo.value.errors

    def test_update_validates_the_merged_row(self):
        size = SystemSizeFactory(price_min=Decimal("100"), price_max=Decimal("200"))
        with pytest.raises(DomainError) as excinfo:
            rows.update_row(rows.SYSTEM_SIZES, size, user=None, data={"price_min": Decimal("300")})
        assert "price_max" in excinfo.value.errors

    def test_no_change_is_no_write(self):
        bank = BankFactory()
        assert rows.update_row(rows.BANKS, bank, user=None, data={"name": bank.name}).version == 1

    def test_conflicts(self):
        BankFactory(slug="sbi")
        with pytest.raises(Conflict):
            rows.create_row(rows.BANKS, user=None, data={"name": "SBI", "abbr": "SBI", "slug": "sbi", "annual_rate": Decimal("0.05")})
        other = BankFactory(slug="federal")
        with pytest.raises(Conflict):
            rows.update_row(rows.BANKS, other, user=None, data={"slug": "sbi"})

    def test_a_rule_the_database_refuses_is_validation_error(self):
        with pytest.raises(DomainError) as excinfo:
            rows.create_row(rows.BANKS, user=None, data={"name": "B", "abbr": "B", "slug": "Not A Slug", "annual_rate": Decimal("0.05")})
        assert excinfo.value.code == "validation_error"

    def test_stale_versions(self):
        rule = SubsidyRuleFactory()
        with pytest.raises(StaleVersion):
            rows.update_row(rows.SUBSIDY_RULES, rule, user=None, data={"amount": Decimal("1")}, expected_version=3)
        with pytest.raises(StaleVersion):
            rows.delete_row(rows.SUBSIDY_RULES, rule, user=None, expected_version=3)


class TestSettings:
    def test_defaults_until_the_first_edit(self):
        current = emi_settings.current_settings()
        assert current.pk is None and current.uid is None and current.down_payment_min_pct == Decimal("0.1000")

    def test_no_change_does_not_create_the_row(self):
        assert emi_settings.update_settings(user=None, data={"panel_life_years": 25}).pk is None
        assert not EmiSettings.objects.exists()

    def test_edits_the_stored_row(self):
        EmiSettingsFactory()
        updated = emi_settings.update_settings(user=None, data={"panel_life_years": 30}, expected_version=1)
        assert updated.version == 2 and EmiSettings.objects.get().panel_life_years == 30

    def test_a_concurrent_first_edit_is_not_overwritten(self):
        emi_settings.update_settings(user=None, data={"panel_life_years": 30}, expected_version=1)
        with pytest.raises(StaleVersion):
            emi_settings.update_settings(user=None, data={"panel_life_years": 20}, expected_version=1)

    def test_every_rule(self):
        defaults = {name: getattr(EmiSettings(), name) for name in emi_settings.FIELDS}
        for change, field in (
            ({"tenure_min_years": 0}, "tenure_min_years"),
            ({"price_step": Decimal("0")}, "price_step"),
            ({"down_payment_max_pct": Decimal("1.5")}, "down_payment_max_pct"),
            ({"down_payment_step_pct": Decimal("0")}, "down_payment_step_pct"),
            ({"daily_saving_divisor": 0}, "daily_saving_divisor"),
        ):
            with pytest.raises(DomainError) as excinfo:
                emi_settings.validate({**defaults, **change})
            assert field in excinfo.value.errors


class TestPriceSources:
    def test_manual_is_the_default(self):
        SystemSizeFactory(capacity_kw=Decimal("3"))
        SystemSizeFactory(capacity_kw=Decimal("5"), is_active=False)
        assert price_sources.source() == "MANUAL"
        assert [option.capacity_kw for option in price_sources.active_sizes()] == [Decimal("3.00")]
        assert price_sources.cache_namespaces() == ()

    @override_settings(EMI_PRICE_SOURCE="PACK_RELEASE")
    def test_pack_release_without_a_provider_is_503(self):
        with pytest.raises(DomainError) as excinfo:
            price_sources.active_sizes()
        assert excinfo.value.status == 503 and excinfo.value.code == "emi_prices_unavailable"

    @override_settings(EMI_PRICE_SOURCE="PACK_RELEASE")
    def test_pack_release_reads_the_registered_provider(self):
        SystemSizeFactory(capacity_kw=Decimal("3"))

        @price_sources.register(cache_namespaces=("packs",))
        def provider():
            return [
                price_sources.SizeOption(uid="b", label="5kW", capacity_kw=Decimal("5.00"), price_per_kw=Decimal("60000.00"), system_cost=Decimal("300000.00"), sort_order=2),
                price_sources.SizeOption(uid="a", label="3kW", capacity_kw=Decimal("3.00"), price_per_kw=Decimal("70000.00"), sort_order=1),
                price_sources.SizeOption(uid="c", label="off", capacity_kw=Decimal("9.00"), price_per_kw=Decimal("1"), is_active=False),
            ]

        assert [option.uid for option in price_sources.active_sizes()] == ["a", "b"]
        assert price_sources.cache_namespaces() == ("packs",) and calculator.cache_namespaces() == ["emi:config", "packs"]


class TestChecks:
    @override_settings(EMI_PRICE_SOURCE="BOTH")
    def test_unknown_source_is_an_error(self):
        assert [message.id for message in checks.check_price_source()] == ["emi.E001"]

    @override_settings(EMI_PRICE_SOURCE="PACK_RELEASE")
    def test_pack_release_without_provider_is_a_warning(self):
        assert [message.id for message in checks.check_price_source()] == ["emi.W001"]
        price_sources.register(lambda: [])
        assert checks.check_price_source() == []

    def test_manual_is_fine(self):
        assert checks.check_price_source() == []


class TestSnapshot:
    def test_rules_outside_their_dates_or_inactive_are_left_out(self):
        InterestRateRuleFactory(label="now")
        InterestRateRuleFactory(label="future", effective_from=dt.date(2027, 1, 1))
        InterestRateRuleFactory(label="past", effective_to=dt.date(2025, 12, 31))
        InterestRateRuleFactory(label="off", is_active=False)
        SubsidyRuleFactory(label="now", amount_per_kw=Decimal("1000"), cap_amount=Decimal("78000"))
        config = calculator.load_config(dt.date(2026, 9, 29))
        assert [rule.label for rule in config.interest_rules] == ["now"]
        assert config.interest_rules[0].rate == Decimal("8.00") and config.subsidy_rules[0].amount_per_kw == Decimal("1000.00")
        assert config.settings.down_payment_min_percent == Decimal("10.00") and config.settings.default_interest_rate == Decimal("9.50")

    def test_public_config_lists_active_banks_in_order(self):
        BankFactory(slug="b", sort_order=2)
        BankFactory(slug="a", sort_order=1, annual_rate=Decimal("0.09"))
        BankFactory(slug="c", sort_order=1, annual_rate=Decimal("0.05"))
        BankFactory(slug="off", is_active=False)
        assert [bank.slug for bank in calculator.public_config()["banks"]] == ["c", "a", "b"]
        assert Bank.objects.count() == 4

    def test_cache_failures_fall_back_to_the_database(self, monkeypatch):
        from django.core.cache import cache

        SystemSizeFactory(capacity_kw=Decimal("3"))

        def broken(*args, **kwargs):
            raise ConnectionError("redis down")

        monkeypatch.setattr(cache, "get", broken)
        monkeypatch.setattr(cache, "set", broken)
        assert calculator.config_snapshot().sizes[0].capacity_kw == Decimal("3.00")
