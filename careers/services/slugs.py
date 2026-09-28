"""Slugs derived from a title or name: always within the slug rule (``careers.models.department.SLUG_REGEX``).

Django's ``slugify`` keeps underscores (``"R&D_Lab"`` → ``"rd_lab"``), which the database CHECK refuses. The server
derives the shape the Studio's own ``slugify`` shows editors: accents folded to ASCII, lower-case, every run of other
characters → one ``-``, trimmed (``"R&D_Lab"`` → ``"r-d-lab"``, ``"Café"`` → ``"cafe"``).
"""

from __future__ import annotations

import re
import unicodedata

_SEPARATORS = re.compile(r"[^a-z0-9]+")


def derive_slug(text: str, max_length: int) -> str:
    """``""`` when nothing usable is left (the caller then asks for an explicit slug)."""
    folded = unicodedata.normalize("NFKD", text or "").encode("ascii", "ignore").decode("ascii").lower()
    return _SEPARATORS.sub("-", folded).strip("-")[:max_length].strip("-")
