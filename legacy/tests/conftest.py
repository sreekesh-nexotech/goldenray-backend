import json

import pytest

from core.flags import FLAGS_CACHE_NAMESPACE
from flarize.cache_utils import bump


@pytest.fixture(autouse=True)
def shim_on(settings, db):
    """Every legacy test runs with ``LEGACY_API_SHIM`` on (the flag tests switch it off) and without throttling (the
    throttle tests set their own rates); FRONTEND_BASE_URL as the legacy CMS golden files were captured with."""
    settings.FEATURE_FLAG_DEFAULTS = {**settings.FEATURE_FLAG_DEFAULTS, "LEGACY_API_SHIM": True}
    rates = {scope: "100000/min" for scope in settings.REST_FRAMEWORK["DEFAULT_THROTTLE_RATES"]}
    settings.REST_FRAMEWORK = {**settings.REST_FRAMEWORK, "DEFAULT_THROTTLE_RATES": rates}
    settings.FRONTEND_BASE_URL = "http://localhost:3000"
    bump(FLAGS_CACHE_NAMESPACE)


def ordered(response):
    """The JSON body with key order kept (``list(body)`` is the wire order)."""
    return json.loads(response.content)


def throttled(settings, scope: str, rate: str):
    rates = {**settings.REST_FRAMEWORK["DEFAULT_THROTTLE_RATES"], scope: rate}
    settings.REST_FRAMEWORK = {**settings.REST_FRAMEWORK, "DEFAULT_THROTTLE_RATES": rates}
