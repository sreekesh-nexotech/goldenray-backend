"""Real Chromium: render the Malayalam publish report end to end and check the PDF (PLAN §1.4 "Documents").

Skipped only when no Chromium can be launched on this machine (the documents worker image always has one).
"""

import glob
import os

import pytest

from documents.models import RenderJob
from documents.services import jobs
from documents.services.renderers import PlaywrightRenderer, TransientRenderError, count_pages
from documents.tests.factories import render

pytestmark = [pytest.mark.playwright, pytest.mark.django_db]

MALAYALAM_PAYLOAD = {
    "title": "വില റിലീസ് 12",
    "reference": "PR-12",
    "generated_at": "2026-09-28 10:00",
    "summary": [{"label": "ഘടകങ്ങൾ", "value": "148"}, {"label": "പാക്കുകൾ", "value": "36"}],
    "sections": [
        {"title": "വിലകൾ", "items": [{"status": "ok", "message": "എല്ലാ സജീവ ഘടകങ്ങൾക്കും വിലയുണ്ട്."}]},
        {"title": "ഓഫറുകൾ", "items": [{"status": "warning", "message": "ഒരു ഓഫർ അടുത്ത ആഴ്ച അവസാനിക്കും."}]},
    ],
    "notes": "ഈ റിപ്പോർട്ട് സ്വയമേവ തയ്യാറാക്കിയതാണ്.",
}


def _launchable_executable() -> str | None:
    """ "" = Playwright's own browser; else a preinstalled Chromium under PLAYWRIGHT_BROWSERS_PATH; None = none."""
    from django.conf import settings

    candidates = [settings.DOCUMENTS_CHROMIUM_EXECUTABLE or ""]
    root = os.environ.get("PLAYWRIGHT_BROWSERS_PATH", "/opt/pw-browsers")
    candidates += sorted(glob.glob(f"{root}/chromium-*/chrome-linux*/chrome"), reverse=True)
    for candidate in candidates:
        try:
            PlaywrightRenderer(executable=candidate or None, timeout_seconds=30, allowed_hosts=[]).render("<p>probe</p>")
            return candidate
        except TransientRenderError:
            continue
    return None


@pytest.fixture(scope="module")
def chromium():
    executable = _launchable_executable()
    if executable is None:
        pytest.skip("No Chromium can be launched on this machine.")
    return executable


def test_malayalam_publish_report_renders_to_a_pdf(chromium, settings, make_user, document_storage):
    settings.DOCUMENTS_RENDERER = "playwright"
    settings.DOCUMENTS_CHROMIUM_EXECUTABLE = chromium
    job = render(make_user(), language="ml", payload=MALAYALAM_PAYLOAD)
    assert jobs.run_job(str(job.uid)) == RenderJob.Status.DONE
    job.refresh_from_db()
    pdf = (document_storage / "private" / job.file).read_bytes()
    assert pdf.startswith(b"%PDF-") and b"%%EOF" in pdf[-1024:]
    assert job.page_count == count_pages(pdf) >= 1
    assert len(pdf) > 2000  # real glyph output, not an empty page


def test_blocked_requests_do_not_break_rendering(chromium):
    html = '<p>ok</p><img src="http://127.0.0.1:9/internal.png"><img src="https://example.invalid/x.png">'
    pdf = PlaywrightRenderer(executable=chromium or None, timeout_seconds=30, allowed_hosts=[]).render(html)
    assert count_pages(pdf) == 1
