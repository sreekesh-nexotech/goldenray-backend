"""Reserved folders: files a bounded context manages through its own endpoints, hidden from the media library.

The media library (``/api/<version>/media/``) is the Content Manager's shared shelf. Files that belong to a record
elsewhere — rendered documents, job-application resumes, employee photos, site-inspection photos — live in a folder
their owning app reserves at startup::

    from media import folders
    folders.reserve("documents")          # AppConfig.ready()

The library never lists, returns, edits, deletes or signs assets in a reserved folder (or below it), and nobody can
upload into one through the library. Their owning context serves them with its own permission checks (for example
``documents/download/<token>/``), so holding ``media.view`` never exposes a resume or an issued agreement.
"""

from __future__ import annotations

import re

from django.db.models import Q

FOLDER_RE = re.compile(r"^[a-z0-9][a-z0-9_-]*(/[a-z0-9][a-z0-9_-]*)*$")
_RESERVED: set[str] = set()


def validate(folder: str) -> str:
    """Normalised folder (lower-case segments of ``a-z0-9_-`` joined by ``/``), or ``ValueError``."""
    folder = (folder or "").strip().strip("/").lower()
    if folder and (len(folder) > 120 or not FOLDER_RE.match(folder)):
        raise ValueError("Use lower-case letters, digits, '-' and '_' in '/'-separated segments (max 120 characters).")
    return folder


def reserve(prefix: str) -> None:
    prefix = validate(prefix)
    if not prefix:
        raise ValueError("A reserved folder cannot be empty.")
    _RESERVED.add(prefix)


def release(prefix: str) -> None:
    _RESERVED.discard(validate(prefix))


def reserved() -> frozenset[str]:
    return frozenset(_RESERVED)


def is_reserved(folder: str) -> bool:
    return any(folder == prefix or folder.startswith(prefix + "/") for prefix in _RESERVED)


def library_q() -> Q:
    """Filter excluding every reserved folder and its sub-folders."""
    condition = Q()
    for prefix in sorted(_RESERVED):
        condition &= ~Q(folder=prefix) & ~Q(folder__startswith=prefix + "/")
    return condition
