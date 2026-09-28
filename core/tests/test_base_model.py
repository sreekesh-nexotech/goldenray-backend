import pytest
from django.contrib.auth.models import AnonymousUser
from django.db import IntegrityError

from accounts.models import Role
from core.errors import DomainError, StaleVersion
from core.models import FeatureFlag, LegacyMap, SequenceCounter
from core.services import check_version, create_stamped, parse_expected_version, save_versioned, stamp_create, stamp_update
from core.tests.factories import FeatureFlagFactory

pytestmark = pytest.mark.django_db


def test_base_columns():
    flag = FeatureFlagFactory()
    assert flag.uid is not None and flag.version == 1 and flag.deleted_at is None
    assert flag.created_at is not None and flag.updated_at is not None
    assert {field.name for field in FeatureFlag._meta.get_fields()} >= {"id", "uid", "created_at", "updated_at", "created_by", "updated_by", "deleted_at", "version"}


def test_soft_delete_and_restore(make_user):
    user = make_user()
    flag = FeatureFlagFactory()
    flag.soft_delete(user)
    assert flag.deleted_at is not None and flag.version == 2 and flag.updated_by == user
    assert not FeatureFlag.objects.filter(pk=flag.pk).exists()
    assert FeatureFlag.all_objects.filter(pk=flag.pk).exists()
    assert FeatureFlag.all_objects.deleted().count() == 1
    flag.soft_delete(user)
    assert flag.version == 2  # idempotent
    flag.restore(user)
    assert flag.deleted_at is None and flag.version == 3
    assert FeatureFlag.objects.filter(pk=flag.pk).exists()


def test_live_rows_enforce_the_partial_unique_key():
    FeatureFlagFactory(key="LEGACY_API_SHIM")
    with pytest.raises(IntegrityError):
        FeatureFlagFactory(key="LEGACY_API_SHIM")


def test_soft_deleted_rows_release_their_key():
    first = FeatureFlagFactory(key="LEGACY_API_SHIM")
    first.soft_delete()
    second = FeatureFlagFactory(key="LEGACY_API_SHIM")
    assert second.pk != first.pk


def test_versioned_update_is_a_compare_and_swap():
    flag = FeatureFlagFactory()
    stale = FeatureFlag.objects.get(pk=flag.pk)
    flag.versioned_update(None, enabled=True)
    assert flag.version == 2 and FeatureFlag.objects.get(pk=flag.pk).enabled is True
    with pytest.raises(StaleVersion):
        stale.versioned_update(None, enabled=False)
    assert FeatureFlag.objects.get(pk=flag.pk).enabled is True


def test_versioned_update_accepts_foreign_key_instances(make_user):
    user = make_user()
    role = Role.objects.create(slug="other", name="Other")
    user.versioned_update(None, role=role)
    assert type(user).objects.get(pk=user.pk).role == role


def test_versioned_update_needs_a_saved_instance():
    with pytest.raises(ValueError):
        FeatureFlag(key="x").versioned_update(None, enabled=True)


def test_save_versioned_and_check_version(make_user):
    user = make_user()
    flag = FeatureFlagFactory()
    flag.enabled = True
    save_versioned(flag, user=user, fields=["enabled"], expected_version=1)
    assert flag.version == 2 and flag.updated_by == user
    check_version(flag, None)
    check_version(flag, "2")
    with pytest.raises(StaleVersion) as excinfo:
        save_versioned(flag, user=user, fields=["enabled"], expected_version=1)
    assert excinfo.value.errors == {"expected_version": ["Current version is 2."]}


@pytest.mark.parametrize("value", ["abc", 0, -1, True, 1.5j])
def test_parse_expected_version_rejects_garbage(value):
    with pytest.raises(DomainError) as excinfo:
        parse_expected_version(value)
    assert excinfo.value.code == "validation_error"


def test_parse_expected_version_accepts_absent_values():
    assert parse_expected_version(None) is None
    assert parse_expected_version("") is None
    assert parse_expected_version("3") == 3


def test_save_adds_updated_at_to_update_fields():
    flag = FeatureFlagFactory()
    before = flag.updated_at
    flag.note = "changed"
    flag.save(update_fields=["note"])
    assert FeatureFlag.objects.get(pk=flag.pk).updated_at > before


def test_stamping_is_explicit(make_user):
    user = make_user()
    flag = stamp_create(FeatureFlag(key="ADMS_RECEIVER"), user)
    assert flag.created_by == user and flag.updated_by == user
    stamp_update(flag, AnonymousUser())
    assert flag.updated_by is None
    created = create_stamped(FeatureFlag, user=user, key="INVENTORY_STOCK")
    assert created.pk and created.created_by == user
    with pytest.raises(TypeError):
        stamp_create(LegacyMap(), user)


def test_no_base_tables_have_no_soft_delete():
    assert not hasattr(SequenceCounter, "deleted_at")
    assert not hasattr(LegacyMap, "all_objects")
