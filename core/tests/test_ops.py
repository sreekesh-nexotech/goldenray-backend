"""ops_report (PLAN §5.6) and reencrypt_secrets (Fernet key rotation)."""

import json
from io import StringIO

import pytest
from django.core import mail
from django.core.management import CommandError, call_command

from core import ops_report
from core.tasks import send_ops_report
from documents.services import jobs
from documents.tests.factories import render

pytestmark = pytest.mark.django_db


@pytest.fixture(autouse=True)
def private_root(settings, tmp_path):
    settings.PRIVATE_MEDIA_ROOT = tmp_path / "private"
    settings.PUBLIC_MEDIA_ROOT = tmp_path / "public"
    settings.MEDIA_PUBLIC_BACKEND = "local"
    settings.DOCUMENTS_RENDERER = "stub"


def test_report_has_every_platform_section(make_user):
    job = render(make_user())
    jobs.fail(job.uid, "RenderError: missing font\ntraceback…")
    report = ops_report.build(days=7)
    assert {"outbox", "render_jobs", "audit", "media"} <= set(report["sections"])
    assert report["sections"]["render_jobs"]["by_status"] == {"FAILED": 1}
    assert report["sections"]["render_jobs"]["recent_failures"][0]["error"] == "RenderError: missing font"
    assert report["sections"]["audit"]["rows"] >= 2 and "documents.render_failed" in report["sections"]["audit"]["top_actions"]
    text = ops_report.render_text(report)
    assert "[render_jobs]" in text and "missing font" in text


def test_a_broken_section_does_not_break_the_report():
    ops_report.register("broken")(lambda since: 1 / 0)
    try:
        assert ops_report.build()["sections"]["broken"] == {"error": "ZeroDivisionError"}
    finally:
        ops_report.unregister("broken")


def test_command_text_json_and_email(settings, django_capture_on_commit_callbacks):
    out = StringIO()
    call_command("ops_report", "--days", "3", stdout=out)
    assert "Flarize ops report" in out.getvalue()
    out = StringIO()
    call_command("ops_report", "--json", stdout=out)
    assert "sections" in json.loads(out.getvalue())
    with pytest.raises(CommandError, match="OPS_EMAILS"):
        call_command("ops_report", "--email", stdout=StringIO())
    settings.OPS_EMAILS = ["ops@flarize.com"]
    with django_capture_on_commit_callbacks(execute=True):
        call_command("ops_report", "--email", stdout=StringIO())
    assert mail.outbox[-1].to == ["ops@flarize.com"] and mail.outbox[-1].subject.startswith("Flarize ops report")
    with pytest.raises(CommandError):
        call_command("ops_report", "--days", "0", stdout=StringIO())


def test_weekly_task(settings, django_capture_on_commit_callbacks):
    assert send_ops_report.apply().get() == 0
    settings.OPS_EMAILS = ["ops@flarize.com", ""]
    with django_capture_on_commit_callbacks(execute=True):
        assert send_ops_report.apply().get() == 1
