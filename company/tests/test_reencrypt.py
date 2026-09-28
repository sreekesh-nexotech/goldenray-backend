"""manage.py reencrypt_secrets — Fernet key rotation over every encrypted column (docs/ops/secrets.md)."""

from io import StringIO

import pytest
from cryptography.fernet import Fernet
from django.core.management import CommandError, call_command
from django.db import connection

from company.models import BankAccount, Integration
from company.tests.factories import BankAccountFactory, IntegrationFactory

pytestmark = pytest.mark.django_db


def test_reencrypt_secrets_rotates_to_the_new_key(settings):
    old_key, new_key = settings.FERNET_KEYS[0], Fernet.generate_key().decode()
    BankAccountFactory(account_number="123456789012")
    IntegrationFactory()
    settings.FERNET_KEYS = [new_key, old_key]
    out = StringIO()
    call_command("reencrypt_secrets", "--dry-run", stdout=out)
    assert "company_bank_account.account_number: 1 value(s) (dry run)" in out.getvalue()
    call_command("reencrypt_secrets", stdout=StringIO())
    settings.FERNET_KEYS = [new_key]  # the old key is gone: everything must still decrypt
    assert BankAccount.objects.get().account_number == "123456789012"
    assert Integration.objects.get().config["password"] == "smtp-secret-value"


def test_reencrypt_refuses_undecryptable_values(settings):
    BankAccountFactory()
    with connection.cursor() as cursor:
        cursor.execute("UPDATE company_bank_account SET account_number = %s", [Fernet(Fernet.generate_key()).encrypt(b"x").decode()])
    with pytest.raises(CommandError, match="cannot be decrypted"):
        call_command("reencrypt_secrets", stdout=StringIO())
