"""Feature flag reads and gating.

``flag_enabled(key)`` answers from the cache (version-keyed on ``core:flags``), falling back to the
``core_feature_flag`` row and then ``settings.FEATURE_FLAG_DEFAULTS``. Unknown keys are off. Gated routes answer
404 while their flag is off, so a disabled surface is indistinguishable from an absent one.
"""

from __future__ import annotations

from functools import wraps

from django.conf import settings
from django.core.cache import cache
from django.http import Http404

from flarize.cache_utils import build_key, get_versions

FLAGS_CACHE_NAMESPACE = "core:flags"
FLAG_CACHE_TTL_SECONDS = 60


def _default(key: str) -> bool:
    return bool(getattr(settings, "FEATURE_FLAG_DEFAULTS", {}).get(key, False))


def flag_enabled(key: str) -> bool:
    cache_key = build_key("flag", key, versions=get_versions([FLAGS_CACHE_NAMESPACE]))
    try:
        cached = cache.get(cache_key)
    except Exception:  # noqa: BLE001 - cache outage falls through to the database
        cached = None
    if cached is not None:
        return bool(cached)
    from core.models import FeatureFlag

    enabled = FeatureFlag.objects.filter(key=key).values_list("enabled", flat=True).first()
    value = _default(key) if enabled is None else bool(enabled)
    try:
        cache.set(cache_key, value, FLAG_CACHE_TTL_SECONDS)
    except Exception:  # noqa: BLE001
        pass
    return value


def require_flag(key: str):
    """Decorator for plain Django views and view methods: 404 while ``key`` is off."""

    def decorator(view_func):
        @wraps(view_func)
        def wrapper(*args, **kwargs):
            if not flag_enabled(key):
                raise Http404(f"Feature {key} is disabled.")
            return view_func(*args, **kwargs)

        wrapper.required_flag = key
        return wrapper

    return decorator


class FlagRequiredMixin:
    """For DRF views: set ``required_flag``; every method answers 404 ``not_found`` while it is off.

    The check runs in ``initial()`` before authentication/permission checks, so a disabled surface leaks nothing.
    """

    required_flag: str | None = None

    def initial(self, request, *args, **kwargs):
        if self.required_flag and not flag_enabled(self.required_flag):
            raise Http404(f"Feature {self.required_flag} is disabled.")
        super().initial(request, *args, **kwargs)
