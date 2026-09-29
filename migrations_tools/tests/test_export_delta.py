"""``export_delta`` (PLAN §7.7): the platform writes after a cutover, for manual replay after a rollback."""

import datetime as dt
import json
import stat

import pytest
from django.core.management.base import CommandError
from django.utils import timezone

from accounts.models import Role, User
from core.models import LegacyMap
from faqs.models import Faq
from migrations_tools.services import delta
from migrations_tools.tests.conftest import run

pytestmark = pytest.mark.django_db


def test_every_target_table_exists():
    assert [table for tables in delta.TARGETS.values() for table in tables if delta.model_for(table) is None] == []


def test_changes_after_the_cutover_are_exported_with_their_legacy_rows(imported, tmp_path):
    cutover = timezone.now()
    faq = Faq.objects.order_by("pk").first()
    Faq.objects.filter(pk=faq.pk).update(question="Edited after the cutover?", updated_at=cutover + dt.timedelta(minutes=5))
    gone = Faq.objects.order_by("pk")[1]
    Faq.all_objects.filter(pk=gone.pk).update(deleted_at=cutover + dt.timedelta(minutes=6))
    user = User.objects.create_user("new.staff@example.com", role=Role.objects.get(slug="admin"))
    output = tmp_path / "delta.json"
    out = run("export_delta", "--source", "cms", "--since", cutover.isoformat(), "--output", str(output))
    assert "faqs_faq: 1 deleted, 1 updated" in out and "accounts_user: 1 created" in out
    assert stat.S_IMODE(output.stat().st_mode) == 0o600
    payload = json.loads(output.read_text())
    rows = {row["uid"]: row for row in payload["tables"]["faqs_faq"]}
    legacy_id = LegacyMap.objects.get(source_table="faqs_faq", target_id=faq.pk).source_id
    assert rows[str(faq.uid)]["legacy"] == [{"source_system": "CMS", "source_table": "faqs_faq", "source_id": legacy_id}]
    assert rows[str(faq.uid)]["fields"]["question"] == "Edited after the cutover?" and rows[str(gone.uid)]["change"] == "deleted"
    assert isinstance(rows[str(faq.uid)]["fields"]["page"], str)  # foreign keys as uids
    staff = payload["tables"]["accounts_user"][0]
    assert staff["uid"] == str(user.uid) and staff["legacy"] is None and staff["fields"]["password"] == "***"
    until = (cutover + dt.timedelta(minutes=1)).isoformat()
    run("export_delta", "--source", "cms", "--since", cutover.isoformat(), "--until", until, "--output", str(output))
    assert "faqs_faq" not in json.loads(output.read_text())["tables"]  # the FAQ edits fall after --until


def test_nothing_changed(imported, tmp_path):
    out = run("export_delta", "--source", "backend", "--since", (timezone.now() + dt.timedelta(hours=1)).isoformat(), "--output", str(tmp_path / "d.json"))
    assert out.startswith("0 changed rows in 0 tables")


def test_bad_windows(db, tmp_path):
    with pytest.raises(CommandError, match="not an ISO"):
        run("export_delta", "--source", "cms", "--since", "yesterday", "--output", str(tmp_path / "d.json"))
    with pytest.raises(CommandError, match="after --since"):
        run("export_delta", "--source", "cms", "--since", "2026-10-02T00:00:00", "--until", "2026-10-01T00:00:00", "--output", str(tmp_path / "d.json"))
