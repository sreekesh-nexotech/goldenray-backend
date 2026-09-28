"""Deterministic upload bytes shared by the legacy capture scripts and the parity tests (pure Python, no Django)."""

from __future__ import annotations

import io
import zipfile

OLE2_MAGIC = b"\xd0\xcf\x11\xe0\xa1\xb1\x1a\xe1"
MB = 1024 * 1024


def pdf(label: str = "resume") -> bytes:
    body = f"1 0 obj << /Type /Catalog >> endobj % {label}\n".encode()
    return b"%PDF-1.4\n" + body + b"trailer << /Root 1 0 R >>\n%%EOF\n"


def docx() -> bytes:
    output = io.BytesIO()
    with zipfile.ZipFile(output, "w") as archive:
        archive.writestr(zipfile.ZipInfo("[Content_Types].xml", date_time=(2026, 1, 1, 0, 0, 0)), '<?xml version="1.0"?><Types/>')
        archive.writestr(zipfile.ZipInfo("word/document.xml", date_time=(2026, 1, 1, 0, 0, 0)), '<?xml version="1.0"?><w:document/>')
    return output.getvalue()


def doc() -> bytes:
    return OLE2_MAGIC + b"\x00" * 504 + "WordDocument".encode("utf-16-le") + b"\x00" * 1024


def fake_pdf() -> bytes:
    """A ``.pdf`` name on content that is not a PDF (the legacy form checked the name only)."""
    return b"this is plain text pretending to be a pdf"


def big_pdf() -> bytes:
    return pdf("big") + b"0" * (10 * MB)


BUILDERS = {"pdf": pdf, "docx": docx, "doc": doc, "fake_pdf": fake_pdf, "big_pdf": big_pdf, "txt": lambda: b"plain text resume"}


def build(kind: str) -> bytes:
    return BUILDERS[kind]()
