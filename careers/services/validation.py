"""Candidate input rules of the legacy public form (``goldenray.serializers.job_application_serializer``), kept exact.

The website form and the Studio already speak these rules; the public endpoint and the importer share them so a
record means the same thing whichever way it arrived.
"""

from __future__ import annotations

import os
import re

INDIA_MOBILE_RE = re.compile(r"^[6-9][0-9]{9}$")
LINKEDIN_HOST_RE = re.compile(r"(^|\.)linkedin\.com$", re.IGNORECASE)
SCHEME_RE = re.compile(r"^https?://", re.IGNORECASE)
PHONE_NOISE_RE = re.compile(r"[\s\-()+]")

MAX_FILE_BYTES = 10 * 1024 * 1024  # the form's "Max 10MB, PDF / Word"
ALLOWED_FILE_EXTENSIONS = (".pdf", ".doc", ".docx")
COVER_LETTER_MAX = 3000


def indian_mobile(value: str) -> str | None:
    """The 10-digit Indian mobile number in ``value`` (spaces, dashes, brackets, ``+`` and a leading 91 removed)."""
    digits = PHONE_NOISE_RE.sub("", value or "")
    if len(digits) == 12 and digits.startswith("91"):
        digits = digits[2:]
    return digits if INDIA_MOBILE_RE.match(digits) else None


def to_e164(mobile: str) -> str:
    return f"+91{mobile}"


def linkedin_url(value: str) -> str | None:
    """``https://``-prefixed URL when the host is linkedin.com (or a sub-domain), else ``None``."""
    value = (value or "").strip()
    url = value if SCHEME_RE.match(value) else f"https://{value}"
    host = SCHEME_RE.sub("", url).split("/")[0].lower()
    return url if LINKEDIN_HOST_RE.search(host) else None


def website_url(value: str) -> str:
    """Optional portfolio site: ``https://`` added when the scheme is missing; blank stays blank."""
    value = (value or "").strip()
    if not value:
        return ""
    return value if SCHEME_RE.match(value) else f"https://{value}"


def upload_error(name: str, size: int) -> str | None:
    """The legacy upload check (extension and size); content sniffing happens later in ``media``."""
    _, ext = os.path.splitext((name or "").lower())
    if ext not in ALLOWED_FILE_EXTENSIONS:
        return "Only PDF or Word documents are allowed."
    if size > MAX_FILE_BYTES:
        return "File must be under 10MB."
    return None


def download_name(candidate: str, label: str, extension: str) -> str:
    """``Harikrishnan_K_R_Resume.pdf`` rather than whatever was uploaded (legacy ``_download_name``)."""
    slug = re.sub(r"[^A-Za-z0-9]+", "_", candidate or "").strip("_")
    return f"{slug or 'application'}_{label}{extension or ''}"
