"""audit.services.record: fields, masking, actor resolution, transactional behaviour, helpers."""

import uuid
from datetime import datetime
from datetime import timezone as dt_timezone
from decimal import Decimal

import pytest
from django.db import transaction

from accounts.tests.factories import RoleFactory
from audit import context
from audit.models import AuditLog
from audit.services import MASK, changes, is_sensitive_key, mask_sensitive, record, snapshot
from core.service_credentials import ServicePrincipal, issue
from core.services.flags import set_flag

pytestmark = pytest.mark.django_db


def test_records_the_object_and_snapshots(make_user):
    user = make_user()
    entry = record("accounts.thing_done", obj=user, before={"a": 1}, after={"a": 2}, note="because", actor=user)
    row = AuditLog.objects.get(id=entry.id)
    assert (row.action, row.object_type, row.object_uid, row.before, row.after, row.note) == ("accounts.thing_done", "accounts.user", user.uid, {"a": 1}, {"a": 2}, "because")
    assert row.actor_id == user.pk and row.actor_kind == "USER"
    assert str(row).startswith("accounts.thing_done accounts.user:")


def test_explicit_subject_and_json_safe_values():
    uid = uuid.uuid4()
    entry = record("tests.thing", object_type="tests.widget", object_uid=str(uid), after={"price": Decimal("12.50"), "at": datetime(2026, 1, 2, tzinfo=dt_timezone.utc), "ref": uid})
    assert entry.object_uid == uid
    assert AuditLog.objects.get(id=entry.id).after == {"price": "12.50", "at": "2026-01-02T00:00:00Z", "ref": str(uid)}


class TestMasking:
    @pytest.mark.parametrize(
        "key",
        [
            "password",
            "new_password",
            "newPassword",
            "PASSWORD_HASH",
            "token",
            "refresh_token",
            "tokens",
            "secret",
            "client-secret",
            "otp",
            "otp_code",
            "account_number",
            "bankAccountNumber",
            "api_key",
            "apiKey",
            "Authorization",
            # Security review: the platform's own secret names (Bunny's `access_key`, key material) were not masked.
            "access_key",
            "accessKey",
            "BUNNY_STORAGE_ACCESS_KEY",
            "private_key",
            "jwtPrivateKey",
            "passphrase",
        ],
    )
    def test_sensitive_keys(self, key):
        assert is_sensitive_key(key)

    @pytest.mark.parametrize("key", ["footprint", "tokenizer", "email", "name", "account", "number", "api", "prefix", "passport", "key", "access", "private", "storage_zone"])
    def test_ordinary_keys(self, key):
        assert not is_sensitive_key(key)

    def test_masks_recursively_including_lists(self):
        value = {"user": {"email": "a@example.com", "password": "hunter2"}, "items": [{"otp": "123456", "qty": 2}], "api_key": {"nested": "x"}, "note": "ok"}
        assert mask_sensitive(value) == {"user": {"email": "a@example.com", "password": MASK}, "items": [{"otp": MASK, "qty": 2}], "api_key": MASK, "note": "ok"}

    def test_record_masks_before_writing(self):
        entry = record("tests.thing", before={"secret": "s3cr3t"}, after={"headers": {"Authorization": "Bearer abc"}, "bank": {"account_number": "123"}})
        row = AuditLog.objects.get(id=entry.id)
        assert row.before == {"secret": MASK} and row.after == {"headers": {"Authorization": MASK}, "bank": {"account_number": MASK}}
        assert "s3cr3t" not in str(row.before) and "abc" not in str(row.after)


class TestValidation:
    @pytest.mark.parametrize("action", ["", "nodot", "Accounts.Login", "a." + "x" * 70, "accounts.", "accounts.login!"])
    def test_invalid_action(self, action):
        with pytest.raises(ValueError):
            record(action)

    def test_invalid_object_type_and_actor_kind(self):
        with pytest.raises(ValueError):
            record("tests.thing", object_type="Not Valid")
        with pytest.raises(ValueError):
            record("tests.thing", actor_kind="ROBOT")

    def test_snapshots_must_be_dicts(self):
        with pytest.raises(TypeError):
            record("tests.thing", before=["not", "a", "dict"])


class TestActorResolution:
    def test_defaults_to_system_outside_a_request(self):
        entry = record("tests.thing")
        assert entry.actor_id is None and entry.actor_kind == "SYSTEM" and entry.request_id is None and entry.ip is None

    def test_uses_the_bound_context(self, make_user):
        user = make_user()
        request_id = str(uuid.uuid4())
        with context.bind(request_id=request_id, ip="203.0.113.5", actor=user, actor_kind="USER"):
            entry = record("tests.thing")
        assert entry.actor == user and entry.actor_kind == "USER" and str(entry.request_id) == request_id and entry.ip == "203.0.113.5"

    def test_explicit_kind_without_actor_ignores_the_context_actor(self, make_user):
        with context.bind(actor=make_user(), actor_kind="USER"):
            entry = record("tests.thing", actor_kind="SYSTEM")
        assert entry.actor_id is None and entry.actor_kind == "SYSTEM"

    def test_service_principal_is_an_agent(self):
        credential, _ = issue("AGENT", "Office PC 1")
        entry = record("tests.thing", actor=ServicePrincipal(credential))
        assert entry.actor_id is None and entry.actor_kind == "AGENT"

    def test_unsaved_or_anonymous_user_is_not_attributed(self):
        from django.contrib.auth.models import AnonymousUser

        assert record("tests.thing", actor=AnonymousUser()).actor_kind == "SYSTEM"


def test_rows_roll_back_with_the_business_write():
    with pytest.raises(RuntimeError):
        with transaction.atomic():
            record("tests.rolled_back")
            raise RuntimeError
    assert not AuditLog.objects.filter(action="tests.rolled_back").exists()


def test_snapshot_and_changes(make_user):
    role = RoleFactory(slug="snap")
    user = make_user(role=role, first_name="A")
    snap = snapshot(user, ["first_name", "role", "is_active"])
    assert snap == {"first_name": "A", "role": str(role.uid), "is_active": True}
    assert changes({"a": 1, "b": 2}, {"a": 1, "b": 3, "c": 4}) == ({"b": 2, "c": None}, {"b": 3, "c": 4})
    assert snapshot(RoleFactory(), ["created_by"]) == {"created_by": None}


class TestCoreWritesAreAudited:
    def test_feature_flags(self, make_user):
        user = make_user()
        set_flag("LEGACY_API_SHIM", enabled=True, user=user, note="cutover C1")
        entry = AuditLog.objects.get(action="core.flag_set")
        assert entry.actor_id == user.pk and entry.before == {"enabled": False} and entry.after == {"enabled": True, "note": "cutover C1"}

    def test_service_credentials_never_record_the_token(self, make_user):
        from core.service_credentials import revoke, rotate

        user = make_user()
        credential, token = issue("AGENT", "Office PC", user=user)
        rotate(credential, user=user)
        revoke(credential, user=user)
        actions = list(AuditLog.objects.filter(object_uid=credential.uid).order_by("id").values_list("action", flat=True))
        assert actions == ["core.service_credential_issued", "core.service_credential_rotated", "core.service_credential_revoked"]
        stored = str(list(AuditLog.objects.values_list("before", "after")))
        assert token.split("_", 2)[2] not in stored and credential.token_hash not in stored
        assert AuditLog.objects.get(action="core.service_credential_issued").after["prefix"]
