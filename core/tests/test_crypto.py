import pytest
from cryptography.fernet import Fernet
from django.core.exceptions import ImproperlyConfigured
from django.db import connection, models
from django.test import override_settings
from django.test.utils import isolate_apps

from flarize.crypto import DecryptionError, EncryptedJSONField, EncryptedTextField, decrypt_str, encrypt_str, get_fernet, reencrypt

OLD_KEY = Fernet.generate_key().decode()
NEW_KEY = Fernet.generate_key().decode()


def test_roundtrip_and_ciphertext_differs():
    token = encrypt_str("s3cret")
    assert token != "s3cret"
    assert decrypt_str(token) == "s3cret"
    assert encrypt_str("s3cret") != token  # random IV


@pytest.mark.parametrize("keys", [[], [""], None])
def test_fail_closed_without_keys(keys):
    with override_settings(FERNET_KEYS=keys):
        with pytest.raises(ImproperlyConfigured):
            encrypt_str("x")
        with pytest.raises(ImproperlyConfigured):
            decrypt_str("x")
        with pytest.raises(ImproperlyConfigured):
            get_fernet()


def test_invalid_key_is_a_configuration_error():
    with override_settings(FERNET_KEYS=["not-a-fernet-key"]):
        with pytest.raises(ImproperlyConfigured):
            encrypt_str("x")


def test_rotation_old_tokens_still_decrypt_and_reencrypt_moves_to_primary():
    with override_settings(FERNET_KEYS=[OLD_KEY]):
        token = encrypt_str("payload")
    with override_settings(FERNET_KEYS=[NEW_KEY, OLD_KEY]):
        assert decrypt_str(token) == "payload"
        rotated = reencrypt(token)
    with override_settings(FERNET_KEYS=[NEW_KEY]):
        assert decrypt_str(rotated) == "payload"
        with pytest.raises(DecryptionError):
            decrypt_str(token)


def test_garbage_never_returns_plaintext():
    with pytest.raises(DecryptionError):
        decrypt_str("definitely-not-a-token")
    with pytest.raises(TypeError):
        encrypt_str(123)


def test_field_prep_and_lookups():
    field = EncryptedTextField()
    assert field.get_prep_value(None) is None
    token = field.get_prep_value("abc")
    assert field.from_db_value(token, None, connection) == "abc"
    assert field.get_lookup("isnull") is not None
    with pytest.raises(TypeError):
        field.get_lookup("exact")
    json_field = EncryptedJSONField()
    token = json_field.get_prep_value({"sid": "AC1", "n": 2})
    assert json_field.from_db_value(token, None, connection) == {"n": 2, "sid": "AC1"}
    assert json_field.to_python('{"a": 1}') == {"a": 1}
    with pytest.raises(TypeError):
        json_field.get_lookup("contains")


@pytest.mark.django_db
@isolate_apps("core")
def test_encrypted_fields_roundtrip_through_the_database():
    class Secret(models.Model):
        text = EncryptedTextField(null=True)
        config = EncryptedJSONField(null=True)

        class Meta:
            app_label = "core"
            db_table = "core_test_secret"

    with connection.schema_editor() as editor:
        editor.create_model(Secret)
    row = Secret.objects.create(text="token-123", config={"api_key": "k", "enabled": True})
    with connection.cursor() as cursor:
        cursor.execute("SELECT text, config FROM core_test_secret WHERE id = %s", [row.pk])
        raw_text, raw_config = cursor.fetchone()
    assert "token-123" not in raw_text and "api_key" not in raw_config
    loaded = Secret.objects.get(pk=row.pk)
    assert loaded.text == "token-123"
    assert loaded.config == {"api_key": "k", "enabled": True}
    assert Secret.objects.filter(text__isnull=False).count() == 1
    with override_settings(FERNET_KEYS=[]):
        with pytest.raises(ImproperlyConfigured):
            Secret.objects.get(pk=row.pk)


def test_json_field_helpers():
    import json as _json

    from django.core.serializers.json import DjangoJSONEncoder

    field = EncryptedJSONField()
    assert field.deconstruct()[3] == {}
    custom = EncryptedJSONField(encoder=_json.JSONEncoder)
    assert custom.deconstruct()[3]["encoder"] is _json.JSONEncoder
    assert field.get_prep_value(None) is None
    assert field.from_db_value(None, None, connection) is None
    assert field.to_python("not json") == "not json"
    assert field.to_python({"a": 1}) == {"a": 1}
    assert field.encoder is DjangoJSONEncoder

    class Holder:
        config = {"b": 2}

    field.attname = "config"
    assert field.value_to_string(Holder()) == '{"b": 2}'
    text = EncryptedTextField()
    assert text.from_db_value(None, None, connection) is None


def test_reencrypt_rejects_foreign_tokens():
    with override_settings(FERNET_KEYS=[OLD_KEY]):
        token = encrypt_str("x")
    with override_settings(FERNET_KEYS=[NEW_KEY]):
        with pytest.raises(DecryptionError):
            reencrypt(token)


@pytest.mark.parametrize("stored", ["clé-non-ascii", None, 42])
def test_reencrypt_fails_closed_with_its_own_error_on_any_undecryptable_value(stored):
    """Security review: a non-ASCII or non-string stored value escaped as UnicodeEncodeError/AttributeError, so
    ``reencrypt_secrets`` crashed with a raw traceback instead of its fail-closed CommandError."""
    with pytest.raises(DecryptionError):
        reencrypt(stored)
