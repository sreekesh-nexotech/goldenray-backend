"""Builders for real file bytes used by the media and documents tests (every format is produced, never faked)."""

from __future__ import annotations

import io
import zipfile

from PIL import Image

from documents.services.renderers import StubRenderer

EXIF_IFD = 0x8769
GPS_IFD = 0x8825


def image_bytes(fmt: str = "JPEG", size=(64, 48), *, captured: str | None = None, offset: str | None = None, gps: bool = False, mode: str = "RGB", color=(200, 120, 40)) -> bytes:
    image = Image.new(mode, size, color if mode != "1" else 1)
    exif = Image.Exif()
    if captured:
        exif[0x0132] = captured
        exif.get_ifd(EXIF_IFD)[0x9003] = captured
        if offset:
            exif.get_ifd(EXIF_IFD)[0x9011] = offset
    if gps:
        exif.get_ifd(GPS_IFD).update({1: "N", 2: (9.0, 58.0, 0.0), 3: "E", 4: (76.0, 17.0, 0.0)})
    output = io.BytesIO()
    options = {"exif": exif.tobytes()} if (captured or gps) else {}
    if fmt == "HEIF":
        import pillow_heif  # noqa: F401 - registers the HEIF plugin

        pillow_heif.register_heif_opener()
    image.save(output, fmt, **options)
    return output.getvalue()


def jpeg(**kwargs) -> bytes:
    return image_bytes("JPEG", **kwargs)


def png(**kwargs) -> bytes:
    return image_bytes("PNG", **kwargs)


def webp(**kwargs) -> bytes:
    return image_bytes("WEBP", **kwargs)


def heic(**kwargs) -> bytes:
    return image_bytes("HEIF", **kwargs)


def pdf() -> bytes:
    return StubRenderer().render("<p>test document</p>")


def docx() -> bytes:
    output = io.BytesIO()
    with zipfile.ZipFile(output, "w") as archive:
        archive.writestr("[Content_Types].xml", '<?xml version="1.0"?><Types xmlns="http://schemas.openxmlformats.org/package/2006/content-types"/>')
        archive.writestr("word/document.xml", '<?xml version="1.0"?><w:document xmlns:w="http://schemas.openxmlformats.org/wordprocessingml/2006/main"/>')
    return output.getvalue()


def plain_zip() -> bytes:
    output = io.BytesIO()
    with zipfile.ZipFile(output, "w") as archive:
        archive.writestr("readme.txt", "not a word document")
    return output.getvalue()


OLE2 = b"\xd0\xcf\x11\xe0\xa1\xb1\x1a\xe1"


def doc() -> bytes:
    """An OLE2 compound file header followed by a directory entry named WordDocument (what the sniffer checks)."""
    return OLE2 + b"\x00" * 504 + "WordDocument".encode("utf-16-le") + b"\x00" * 1024


def xls_like() -> bytes:
    return OLE2 + b"\x00" * 504 + "Workbook".encode("utf-16-le") + b"\x00" * 1024
