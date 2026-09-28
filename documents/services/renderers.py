"""HTML → PDF renderers, selected by ``settings.DOCUMENTS_RENDERER``.

* ``playwright`` — headless Chromium through Playwright's sync API (the ``documents`` worker image ships Chromium and
  the Noto Sans / Malayalam / Devanagari fonts the base template declares). A4, backgrounds printed, CSS ``@page``
  honoured. JavaScript is disabled and every network request is blocked except ``https://`` hosts listed in
  ``DOCUMENTS_ALLOWED_ASSET_HOSTS`` (the CDN), so a value in a payload can never make the renderer fetch an
  internal URL.
* ``stub`` — deterministic minimal PDF bytes derived from the HTML (tests, laptops without Chromium).

Errors: :class:`TransientRenderError` (Chromium could not start — worth retrying) and :class:`RenderError`
(the document itself cannot be rendered — retrying cannot help).
"""

from __future__ import annotations

import hashlib
import re
from typing import Protocol
from urllib.parse import urlsplit

from django.conf import settings
from django.core.exceptions import ImproperlyConfigured

PDF_PAGE_RE = re.compile(rb"/Type\s*/Page(?![A-Za-z])")
PDF_COUNT_RE = re.compile(rb"/Count\s+(\d+)")


class RenderError(RuntimeError):
    """The document cannot be rendered (template/content problem). Not retried."""


class TransientRenderError(RenderError):
    """The renderer is temporarily unavailable (browser failed to start). Retried."""


class Renderer(Protocol):
    name: str

    def render(self, html: str) -> bytes: ...


def count_pages(pdf: bytes) -> int:
    """Page count of a PDF (page objects, else the page tree's ``/Count``). Raises ``RenderError`` if not a PDF."""
    if not pdf.startswith(b"%PDF-") or b"%%EOF" not in pdf[-2048:]:
        raise RenderError("The renderer did not return a valid PDF.")
    pages = len(PDF_PAGE_RE.findall(pdf))
    if pages == 0:
        counts = [int(value) for value in PDF_COUNT_RE.findall(pdf)]
        pages = max(counts) if counts else 0
    if pages < 1:
        raise RenderError("The rendered PDF has no pages.")
    return pages


class StubRenderer:
    """A valid one-page PDF whose content line is a digest of the HTML (same HTML → same bytes)."""

    name = "stub"

    def render(self, html: str) -> bytes:
        digest = hashlib.sha256(html.encode("utf-8")).hexdigest()
        stream = f"BT /F1 12 Tf 72 770 Td (Flarize document {digest[:32]}) Tj ET".encode("ascii")
        objects = [
            b"<< /Type /Catalog /Pages 2 0 R >>",
            b"<< /Type /Pages /Kids [3 0 R] /Count 1 >>",
            b"<< /Type /Page /Parent 2 0 R /MediaBox [0 0 595 842] /Resources << /Font << /F1 4 0 R >> >> /Contents 5 0 R >>",
            b"<< /Type /Font /Subtype /Type1 /BaseFont /Helvetica >>",
            b"<< /Length " + str(len(stream)).encode() + b" >>\nstream\n" + stream + b"\nendstream",
        ]
        output = bytearray(b"%PDF-1.4\n%\xe2\xe3\xcf\xd3\n")
        offsets = []
        for number, body in enumerate(objects, start=1):
            offsets.append(len(output))
            output += f"{number} 0 obj\n".encode() + body + b"\nendobj\n"
        xref_at = len(output)
        output += f"xref\n0 {len(objects) + 1}\n0000000000 65535 f \n".encode()
        for offset in offsets:
            output += f"{offset:010d} 00000 n \n".encode()
        output += f"trailer\n<< /Size {len(objects) + 1} /Root 1 0 R >>\nstartxref\n{xref_at}\n%%EOF\n".encode()
        return bytes(output)


class PlaywrightRenderer:
    name = "playwright"

    def __init__(self, *, executable: str | None = None, timeout_seconds: int | None = None, allowed_hosts=None):
        self.executable = executable if executable is not None else (settings.DOCUMENTS_CHROMIUM_EXECUTABLE or None)
        self.timeout_ms = int((timeout_seconds or settings.DOCUMENTS_RENDER_TIMEOUT_SECONDS) * 1000)
        hosts = allowed_hosts if allowed_hosts is not None else settings.DOCUMENTS_ALLOWED_ASSET_HOSTS
        self.allowed_hosts = frozenset(host.strip().lower() for host in hosts if host and host.strip())

    def allows(self, url: str) -> bool:
        parts = urlsplit(url)
        if parts.scheme in ("data", "about"):
            return True
        return parts.scheme == "https" and (parts.hostname or "").lower() in self.allowed_hosts

    def _route(self, route) -> None:
        if self.allows(route.request.url):
            route.continue_()
        else:
            route.abort("blockedbyclient")

    def render(self, html: str) -> bytes:
        from playwright.sync_api import Error as PlaywrightError
        from playwright.sync_api import sync_playwright

        with sync_playwright() as playwright:
            try:
                browser = playwright.chromium.launch(executable_path=self.executable, args=["--disable-dev-shm-usage", "--font-render-hinting=none"])
            except PlaywrightError as exc:
                raise TransientRenderError(f"Chromium could not start: {str(exc).splitlines()[0][:300]}") from exc
            try:
                context = browser.new_context(java_script_enabled=False)
                page = context.new_page()
                page.route("**/*", self._route)
                page.set_content(html, wait_until="load", timeout=self.timeout_ms)
                return page.pdf(format="A4", print_background=True, prefer_css_page_size=True)
            except PlaywrightError as exc:
                raise RenderError(f"Rendering failed: {str(exc).splitlines()[0][:300]}") from exc
            finally:
                browser.close()


def get_renderer() -> Renderer:
    name = settings.DOCUMENTS_RENDERER
    if name == "playwright":
        return PlaywrightRenderer()
    if name == "stub":
        return StubRenderer()
    raise ImproperlyConfigured(f"Unknown DOCUMENTS_RENDERER {name!r} (expected 'playwright' or 'stub').")
