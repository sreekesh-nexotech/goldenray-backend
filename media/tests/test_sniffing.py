"""Content sniffing decides the type from the bytes; EXIF capture time and GPS presence are read."""

import io
from datetime import datetime, timedelta, timezone

import pytest

from media.services import policies
from media.services.sniffing import ImageTooLarge, UnsupportedFile, sniff
from media.tests import files


def _sniff(data: bytes):
    return sniff(io.BytesIO(data))


@pytest.mark.parametrize(
    "builder,mime",
    [
        (files.jpeg, policies.JPEG),
        (files.png, policies.PNG),
        (files.webp, policies.WEBP),
        (files.heic, policies.HEIC),
        (files.pdf, policies.PDF),
        (files.docx, policies.DOCX),
        (files.doc, policies.DOC),
    ],
)
def test_detects_each_accepted_type(builder, mime):
    sniffed = _sniff(builder())
    assert sniffed.mime_type == mime
    assert sniffed.extension == policies.EXTENSIONS[mime]


def test_image_dimensions():
    sniffed = _sniff(files.png(size=(120, 30)))
    assert (sniffed.width, sniffed.height) == (120, 30)


@pytest.mark.parametrize(
    "data,message",
    [
        (b"hello, this is plain text", "not recognised"),
        (b"<svg xmlns='http://www.w3.org/2000/svg'></svg>", "not recognised"),
        (files.pdf()[:-20], "truncated"),
        (files.xls_like(), "Word documents"),
        (files.plain_zip(), "docx"),
        (files.jpeg()[:40], "not recognised"),
    ],
)
def test_rejects_unknown_or_corrupt_content(data, message):
    with pytest.raises(UnsupportedFile, match=message):
        _sniff(data)


def test_decompression_bomb_is_refused(settings):
    settings.MEDIA_MAX_IMAGE_PIXELS = 1_000_000
    with pytest.raises(ImageTooLarge):
        _sniff(files.png(size=(1500, 1500), mode="1"))


def test_exif_capture_time_uses_the_offset_tag():
    sniffed = _sniff(files.jpeg(captured="2025:03:04 10:11:12", offset="+04:00"))
    assert sniffed.captured_at == datetime(2025, 3, 4, 10, 11, 12, tzinfo=timezone(timedelta(hours=4)))


def test_exif_capture_time_without_offset_is_local_time():
    sniffed = _sniff(files.jpeg(captured="2025:03:04 10:11:12"))
    assert sniffed.captured_at.utcoffset() == timedelta(hours=5, minutes=30)
    assert sniffed.captured_at.replace(tzinfo=None) == datetime(2025, 3, 4, 10, 11, 12)


def test_malformed_exif_date_is_ignored():
    assert _sniff(files.jpeg(captured="yesterday")).captured_at is None


def test_gps_presence():
    assert _sniff(files.jpeg(gps=True)).has_location is True
    assert _sniff(files.jpeg()).has_location is False
