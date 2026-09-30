import copy

import pytest
from django.conf import settings
from django.core.exceptions import ImproperlyConfigured
from django.test import override_settings

from flarize.throttles import parse_rate

pytestmark = pytest.mark.urls("core.tests.urls_testing")


@pytest.mark.parametrize(
    ("rate", "expected"),
    [
        ("5/10min", (5, 600)),
        ("10/15min", (10, 900)),
        ("600/min", (600, 60)),
        ("20/m", (20, 60)),
        ("1/s", (1, 1)),
        ("100/hour", (100, 3600)),
        ("3/2h", (3, 7200)),
        ("7/day", (7, 86400)),
        ("2/5mins", (2, 300)),
        (None, (None, None)),
    ],
)
def test_parse_rate(rate, expected):
    assert parse_rate(rate) == expected


@pytest.mark.parametrize("rate", ["5", "x/min", "5/fortnight", "0/min", "5/0min", "-1/min"])
def test_parse_rate_rejects_garbage(rate):
    with pytest.raises(ImproperlyConfigured):
        parse_rate(rate)


def test_production_rates_match_the_plan():
    from flarize.settings import base

    assert base.THROTTLE_RATES == {
        "public_read": "600/min",
        "public_write": "20/min",
        "otp": "5/10min",
        "otp_ip": "20/10min",  # leads: OTP per client IP besides per phone
        "login": "10/15min",
        "token_refresh": "300/15min",  # DV-8
        "staff": "1200/min",
        "agent": "120/min",
        "iclock": "300/min",
        "customer": "60/min",
    }
    assert base.REST_FRAMEWORK["DEFAULT_THROTTLE_CLASSES"] == ["flarize.throttles.ScopedRateThrottle"]


def _rates(**overrides):
    rest = copy.deepcopy(settings.REST_FRAMEWORK)
    rest["DEFAULT_THROTTLE_RATES"].update(overrides)
    return override_settings(REST_FRAMEWORK=rest)


def test_public_read_is_limited_per_client_ip(api_client):
    url = "/api/public/v1/_t/echo/"
    with _rates(public_read="2/min"):
        assert api_client.get(url, REMOTE_ADDR="203.0.113.1").status_code == 200
        assert api_client.get(url, REMOTE_ADDR="203.0.113.1").status_code == 200
        blocked = api_client.get(url, REMOTE_ADDR="203.0.113.1")
        assert blocked.status_code == 429
        assert blocked.json()["code"] == "throttled"
        assert int(blocked["Retry-After"]) >= 1
        assert api_client.get(url, REMOTE_ADDR="203.0.113.2").status_code == 200


def test_spoofed_forwarded_for_from_an_untrusted_peer_does_not_evade(api_client):
    url = "/api/public/v1/_t/echo/"
    with _rates(public_read="1/min"):
        assert api_client.get(url, REMOTE_ADDR="203.0.113.1", HTTP_X_FORWARDED_FOR="1.1.1.1").status_code == 200
        assert api_client.get(url, REMOTE_ADDR="203.0.113.1", HTTP_X_FORWARDED_FOR="2.2.2.2").status_code == 429


def test_trusted_proxy_forwards_the_real_client(api_client):
    url = "/api/public/v1/_t/echo/"
    with _rates(public_read="1/min"):
        assert api_client.get(url, REMOTE_ADDR="10.0.0.1", HTTP_X_FORWARDED_FOR="198.51.100.1").status_code == 200
        assert api_client.get(url, REMOTE_ADDR="10.0.0.1", HTTP_X_FORWARDED_FOR="198.51.100.2").status_code == 200
        assert api_client.get(url, REMOTE_ADDR="10.0.0.1", HTTP_X_FORWARDED_FOR="198.51.100.1").status_code == 429


def test_public_writes_use_the_write_scope(api_client):
    with _rates(public_write="1/min"):
        assert api_client.post("/api/public/v1/_t/things/", {}, format="json").status_code == 201
        assert api_client.post("/api/public/v1/_t/things/", {}, format="json").status_code == 429
        assert api_client.get("/api/public/v1/_t/echo/").status_code == 200


def test_views_can_throttle_on_another_identity(api_client):
    with _rates(otp="1/10min"):
        assert api_client.post("/api/public/v1/_t/otp-like/", {"phone": "+911111111111"}, format="json").status_code == 200
        assert api_client.post("/api/public/v1/_t/otp-like/", {"phone": "+911111111111"}, format="json", REMOTE_ADDR="203.0.113.99").status_code == 429
        assert api_client.post("/api/public/v1/_t/otp-like/", {"phone": "+912222222222"}, format="json").status_code == 200


@pytest.mark.django_db
def test_staff_is_limited_per_user(make_user, auth_client):
    first = auth_client(make_user(grants={"settings": ["view"]}))
    second = auth_client(make_user(grants={"settings": ["view"]}))
    with _rates(staff="1/min"):
        assert first.get("/api/v1/_t/staff-method/").status_code == 200
        assert first.get("/api/v1/_t/staff-method/").status_code == 429
        assert second.get("/api/v1/_t/staff-method/").status_code == 200


def test_scope_without_a_rate_is_a_configuration_error(api_client):
    rest = copy.deepcopy(settings.REST_FRAMEWORK)
    del rest["DEFAULT_THROTTLE_RATES"]["public_read"]
    with override_settings(REST_FRAMEWORK=rest):
        with pytest.raises(ImproperlyConfigured):
            from rest_framework.test import APIRequestFactory

            from core.tests.support import EchoVersionView
            from flarize.throttles import ScopedRateThrottle

            view = EchoVersionView()
            view.throttle_scope = "public_read"
            ScopedRateThrottle().allow_request(APIRequestFactory().get("/"), view)


class _BrokenCache:
    def get(self, *args, **kwargs):
        raise ConnectionError("redis is down")

    def set(self, *args, **kwargs):
        raise ConnectionError("redis is down")


def test_a_cache_outage_fails_open(api_client, monkeypatch, caplog):
    from flarize import throttles

    monkeypatch.setattr(throttles, "default_cache", _BrokenCache())
    with _rates(public_read="1/min"):
        assert [api_client.get("/api/public/v1/_t/echo/").status_code for _ in range(3)] == [200, 200, 200]
    assert "throttle cache unavailable" in caplog.text


@pytest.mark.django_db
def test_a_throttle_defect_is_not_mistaken_for_a_cache_outage(api_client, monkeypatch):
    """Final review: the fail-open used to swallow *every* exception, so a bug inside the throttle (seen as a TypeError
    from a timer captured under freezegun) silently switched rate limiting off for the whole process."""
    from flarize import throttles

    def broken_timer(self):
        raise TypeError("defect")

    monkeypatch.setattr(throttles.ScopedRateThrottle, "timer", broken_timer)
    with _rates(public_read="1/min"):
        assert api_client.get("/api/public/v1/_t/echo/").status_code == 500


def test_the_timer_reads_the_clock_at_call_time():
    """``time.time`` is looked up per call, so a module first imported under freezegun keeps a working clock."""
    import datetime as dt

    from freezegun import freeze_time

    from flarize.throttles import ScopedRateThrottle

    with freeze_time("2026-01-01 00:00:00"):
        assert ScopedRateThrottle().timer() == dt.datetime(2026, 1, 1, tzinfo=dt.timezone.utc).timestamp()
    assert ScopedRateThrottle().timer() > dt.datetime(2026, 1, 2, tzinfo=dt.timezone.utc).timestamp()
