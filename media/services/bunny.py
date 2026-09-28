"""Thin Bunny Storage client (public media only; PLAN §1.3 "Bunny CDN (public media)").

Configuration comes from the enabled ``BUNNY`` integration (``company_integration``, Fernet encrypted, editable by
Admin in Studio) through :mod:`core.integrations`, else from the ``BUNNY_*`` environment settings. Every upload
sends Bunny's ``Checksum`` header (SHA-256), so a corrupted transfer is rejected by Bunny instead of being served.
Errors raise :class:`BunnyError` (the caller turns them into a 502 ``storage_unavailable``); nothing is retried
silently inside a request.
"""

from __future__ import annotations

import hashlib
from dataclasses import dataclass
from urllib.parse import quote

import requests
from django.conf import settings
from django.core.exceptions import ImproperlyConfigured

from core import integrations


class BunnyError(RuntimeError):
    """Bunny answered with an error or could not be reached."""


class BunnyNotFound(BunnyError):
    pass


@dataclass(frozen=True)
class BunnyConfig:
    storage_zone: str
    storage_endpoint: str
    access_key: str
    cdn_base_url: str

    def __repr__(self) -> str:  # never print the access key
        return f"BunnyConfig(storage_zone={self.storage_zone!r}, storage_endpoint={self.storage_endpoint!r}, cdn_base_url={self.cdn_base_url!r})"


def load_config() -> BunnyConfig:
    stored = integrations.get_config(integrations.BUNNY)
    if stored:
        values = {
            "storage_zone": stored.get("storage_zone", ""),
            "storage_endpoint": stored.get("storage_endpoint") or "storage.bunnycdn.com",
            "access_key": stored.get("access_key", ""),
            "cdn_base_url": stored.get("cdn_base_url", ""),
        }
    else:
        values = {
            "storage_zone": settings.BUNNY_STORAGE_ZONE,
            "storage_endpoint": settings.BUNNY_STORAGE_ENDPOINT or "storage.bunnycdn.com",
            "access_key": settings.BUNNY_STORAGE_ACCESS_KEY,
            "cdn_base_url": settings.BUNNY_CDN_BASE_URL,
        }
    missing = [name for name, value in values.items() if not value]
    if missing:
        raise ImproperlyConfigured(f"Bunny storage is not configured (missing: {', '.join(missing)}).")
    if not values["cdn_base_url"].startswith("https://"):
        raise ImproperlyConfigured("Bunny cdn_base_url must be an https:// URL.")
    return BunnyConfig(**values)


class BunnyClient:
    def __init__(self, config: BunnyConfig, *, session: requests.Session | None = None, timeout: float | None = None):
        self.config = config
        self.session = session or requests.Session()
        self.timeout = timeout if timeout is not None else settings.BUNNY_TIMEOUT_SECONDS

    def storage_url(self, key: str) -> str:
        return f"https://{self.config.storage_endpoint}/{quote(self.config.storage_zone)}/{quote(key)}"

    def cdn_url(self, key: str) -> str:
        return f"{self.config.cdn_base_url.rstrip('/')}/{quote(key)}"

    def _request(self, method: str, key: str, **kwargs) -> requests.Response:
        headers = {"AccessKey": self.config.access_key, **kwargs.pop("headers", {})}
        try:
            return self.session.request(method, self.storage_url(key), headers=headers, timeout=self.timeout, **kwargs)
        except requests.RequestException as exc:
            raise BunnyError(f"Bunny {method} failed: {exc.__class__.__name__}") from exc

    def put(self, key: str, data: bytes) -> None:
        checksum = hashlib.sha256(data).hexdigest().upper()
        response = self._request("PUT", key, data=data, headers={"Content-Type": "application/octet-stream", "Checksum": checksum})
        if response.status_code not in (200, 201):
            raise BunnyError(f"Bunny upload refused ({response.status_code}).")

    def get(self, key: str) -> bytes:
        response = self._request("GET", key)
        if response.status_code == 404:
            raise BunnyNotFound(f"{key} is not in the storage zone.")
        if response.status_code != 200:
            raise BunnyError(f"Bunny download refused ({response.status_code}).")
        return response.content

    def delete(self, key: str) -> None:
        response = self._request("DELETE", key)
        if response.status_code not in (200, 404):
            raise BunnyError(f"Bunny delete refused ({response.status_code}).")


def client() -> BunnyClient:
    return BunnyClient(load_config())
