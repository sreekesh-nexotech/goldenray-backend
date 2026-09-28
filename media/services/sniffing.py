"""Content sniffing: the file type is decided by the bytes, never by the client's filename or Content-Type.

* images (JPEG, PNG, WebP, HEIC/HEIF) are opened with Pillow (HEIC through ``pillow-heif``) and verified; width,
  height, EXIF ``captured_at`` and the presence of GPS data are read. Decompression bombs are refused.
* PDF: ``%PDF-`` header and an ``%%EOF`` marker near the end (catches truncated uploads).
* DOC: OLE2 compound file containing a ``WordDocument`` stream (not any OLE2 file — .xls/.msi are refused).
* DOCX: a ZIP whose entries include ``[Content_Types].xml`` and ``word/document.xml`` (names only; never extracted).
"""

from __future__ import annotations

import warnings
import zipfile
from dataclasses import dataclass
from datetime import datetime, timedelta
from datetime import timezone as dt_timezone
from typing import BinaryIO

from django.conf import settings
from django.utils import timezone
from PIL import Image, UnidentifiedImageError

from media.services.policies import DOC, DOCX, EXTENSIONS, HEIC, JPEG, PDF, PNG, WEBP

try:  # HEIC/HEIF decoding (iPhone photos). Registered once; Pillow then opens HEIF like any other format.
    import pillow_heif

    pillow_heif.register_heif_opener()
    HEIF_SUPPORTED = True
except ImportError:  # pragma: no cover - the dependency is pinned in requirements/base.txt
    HEIF_SUPPORTED = False

OLE2_MAGIC = b"\xd0\xcf\x11\xe0\xa1\xb1\x1a\xe1"
WORD_STREAM = "WordDocument".encode("utf-16-le")
PDF_EOF_WINDOW = 2048
OLE2_SCAN_BYTES = 4 * 1024 * 1024
PIL_FORMATS = {"JPEG": JPEG, "PNG": PNG, "WEBP": WEBP, "HEIF": HEIC}

EXIF_IFD = 0x8769
GPS_IFD = 0x8825
TAG_DATETIME = 0x0132
TAG_DATETIME_ORIGINAL = 0x9003
TAG_OFFSET_TIME = 0x9010
TAG_OFFSET_TIME_ORIGINAL = 0x9011


class UnsupportedFile(ValueError):
    """The content is not a file type we accept (or is corrupt)."""


class ImageTooLarge(ValueError):
    """The image exceeds ``MEDIA_MAX_IMAGE_PIXELS`` (decompression bomb guard)."""


@dataclass(frozen=True)
class Sniffed:
    mime_type: str
    width: int | None = None
    height: int | None = None
    captured_at: datetime | None = None
    has_location: bool = False
    image_format: str = ""

    @property
    def extension(self) -> str:
        return EXTENSIONS[self.mime_type]


def _read_head(fileobj: BinaryIO, size: int) -> bytes:
    fileobj.seek(0)
    head = fileobj.read(size)
    fileobj.seek(0)
    return head


def _tail(fileobj: BinaryIO, size: int) -> bytes:
    fileobj.seek(0, 2)
    end = fileobj.tell()
    fileobj.seek(max(0, end - size))
    tail = fileobj.read(size)
    fileobj.seek(0)
    return tail


def _parse_offset(value) -> dt_timezone | None:
    text = str(value or "").strip().strip("\x00")
    if len(text) != 6 or text[0] not in "+-" or text[3] != ":":
        return None
    try:
        hours, minutes = int(text[1:3]), int(text[4:6])
    except ValueError:
        return None
    delta = timedelta(hours=hours, minutes=minutes)
    return dt_timezone(delta if text[0] == "+" else -delta)


def exif_captured_at(image: Image.Image) -> datetime | None:
    """``DateTimeOriginal`` (else ``DateTime``) as an aware datetime; the offset tag wins, else ``TIME_ZONE``."""
    try:
        exif = image.getexif()
    except Exception:  # noqa: BLE001 - malformed EXIF never fails an upload
        return None
    sub = exif.get_ifd(EXIF_IFD) if EXIF_IFD in exif else {}
    raw, offset = sub.get(TAG_DATETIME_ORIGINAL), sub.get(TAG_OFFSET_TIME_ORIGINAL)
    if not raw:
        raw, offset = exif.get(TAG_DATETIME), sub.get(TAG_OFFSET_TIME)
    if not raw:
        return None
    try:
        naive = datetime.strptime(str(raw).strip().strip("\x00"), "%Y:%m:%d %H:%M:%S")
    except ValueError:
        return None
    tz = _parse_offset(offset)
    if tz is not None:
        return naive.replace(tzinfo=tz)
    return timezone.make_aware(naive, timezone.get_default_timezone())


def exif_has_location(image: Image.Image) -> bool:
    try:
        exif = image.getexif()
        return GPS_IFD in exif and bool(exif.get_ifd(GPS_IFD))
    except Exception:  # noqa: BLE001
        return False


def open_image(fileobj: BinaryIO) -> Image.Image:
    """Open an image with the decompression-bomb limit applied (warning → error)."""
    fileobj.seek(0)
    previous = Image.MAX_IMAGE_PIXELS
    Image.MAX_IMAGE_PIXELS = int(getattr(settings, "MEDIA_MAX_IMAGE_PIXELS", 50_000_000))
    try:
        with warnings.catch_warnings():
            warnings.simplefilter("error", Image.DecompressionBombWarning)
            try:
                return Image.open(fileobj)
            except (Image.DecompressionBombError, Image.DecompressionBombWarning) as exc:
                raise ImageTooLarge(str(exc)) from exc
    finally:
        Image.MAX_IMAGE_PIXELS = previous


def _sniff_image(fileobj: BinaryIO) -> Sniffed | None:
    try:
        image = open_image(fileobj)
    except ImageTooLarge:
        raise
    except (UnidentifiedImageError, OSError, SyntaxError, ValueError):
        return None
    mime = PIL_FORMATS.get(image.format or "")
    if mime is None:
        return None
    width, height = image.size
    if width * height > int(getattr(settings, "MEDIA_MAX_IMAGE_PIXELS", 50_000_000)):
        raise ImageTooLarge(f"{width}x{height} exceeds the pixel limit.")
    try:
        image.verify()  # structural check; the image object is unusable afterwards, so metadata is read from a fresh open
    except Exception as exc:  # noqa: BLE001 - any verification failure means a corrupt image
        raise UnsupportedFile("The image is corrupt.") from exc
    finally:
        fileobj.seek(0)
    image = open_image(fileobj)
    try:
        return Sniffed(mime_type=mime, width=width, height=height, captured_at=exif_captured_at(image), has_location=exif_has_location(image), image_format=image.format or "")
    finally:
        fileobj.seek(0)


def _is_docx(fileobj: BinaryIO) -> bool:
    try:
        with zipfile.ZipFile(fileobj) as archive:
            names = set(archive.namelist())
    except (zipfile.BadZipFile, OSError, ValueError):
        return False
    finally:
        fileobj.seek(0)
    return "[Content_Types].xml" in names and "word/document.xml" in names


def sniff(fileobj: BinaryIO) -> Sniffed:
    """Identify ``fileobj`` by content. Raises :class:`UnsupportedFile` or :class:`ImageTooLarge`."""
    head = _read_head(fileobj, 16)
    if head.startswith(b"%PDF-"):
        if b"%%EOF" not in _tail(fileobj, PDF_EOF_WINDOW):
            raise UnsupportedFile("The PDF is truncated or corrupt.")
        return Sniffed(mime_type=PDF)
    if head.startswith(OLE2_MAGIC):
        if WORD_STREAM in _read_head(fileobj, OLE2_SCAN_BYTES):
            return Sniffed(mime_type=DOC)
        raise UnsupportedFile("Only Word documents are accepted among Office binary files.")
    if head.startswith(b"PK\x03\x04"):
        if _is_docx(fileobj):
            return Sniffed(mime_type=DOCX)
        raise UnsupportedFile("Only Word (.docx) documents are accepted among ZIP-based files.")
    sniffed = _sniff_image(fileobj)
    if sniffed is None:
        raise UnsupportedFile("The file type is not recognised.")
    return sniffed
