"""Renderer selection, the deterministic stub, page counting and the Playwright network allow-list."""

import pytest
from django.core.exceptions import ImproperlyConfigured

from documents.services import renderers
from documents.services.renderers import PlaywrightRenderer, RenderError, StubRenderer, count_pages, get_renderer


def test_stub_is_deterministic_and_valid():
    first = StubRenderer().render("<p>a</p>")
    assert first == StubRenderer().render("<p>a</p>") != StubRenderer().render("<p>b</p>")
    assert first.startswith(b"%PDF-1.4") and first.rstrip().endswith(b"%%EOF") and count_pages(first) == 1


def test_count_pages():
    assert count_pages(b"%PDF-1.7\n1 0 obj << /Type /Pages /Count 3 >> endobj\n%%EOF") == 3
    assert count_pages(b"%PDF-1.4\n<< /Type /Page >> << /Type/Page >> << /Type /Pages /Count 2 >>\n%%EOF") == 2
    for bad in (b"", b"<html>", b"%PDF-1.4 no end", b"%PDF-1.4\n%%EOF"):
        with pytest.raises(RenderError):
            count_pages(bad)


def test_selection(settings):
    settings.DOCUMENTS_RENDERER = "stub"
    assert isinstance(get_renderer(), StubRenderer)
    settings.DOCUMENTS_RENDERER = "playwright"
    assert isinstance(get_renderer(), PlaywrightRenderer)
    settings.DOCUMENTS_RENDERER = "wkhtmltopdf"
    with pytest.raises(ImproperlyConfigured):
        get_renderer()


@pytest.mark.parametrize(
    "url,allowed",
    [
        ("data:image/png;base64,AAAA", True),
        ("about:blank", True),
        ("https://cdn.flarize.test/logo.png", True),
        ("https://CDN.flarize.test/logo.png", True),
        ("http://cdn.flarize.test/logo.png", False),
        ("https://evil.test/x.png", False),
        ("http://127.0.0.1:8000/api/v1/users/", False),
        ("http://169.254.169.254/latest/meta-data/", False),
        ("file:///etc/passwd", False),
    ],
)
def test_network_allow_list(url, allowed):
    assert PlaywrightRenderer(allowed_hosts=["cdn.flarize.test"], executable="", timeout_seconds=5).allows(url) is allowed


def test_launch_failure_is_transient(monkeypatch):
    renderer = PlaywrightRenderer(executable="/nonexistent/chrome", timeout_seconds=5, allowed_hosts=[])
    with pytest.raises(renderers.TransientRenderError, match="Chromium could not start"):
        renderer.render("<p>x</p>")
