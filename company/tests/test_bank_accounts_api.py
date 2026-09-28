"""company/bank-accounts/ — CRUD, one primary, make-primary, encrypted account number never returned in full."""

import pytest
from django.db import connection

from audit.models import AuditLog
from company.models import BankAccount
from company.services.bank_accounts import primary_account
from company.tests.factories import BankAccountFactory

pytestmark = pytest.mark.django_db
URL = "/api/v1/company/bank-accounts/"
NEW = {"label": "Current account", "bank": "HDFC Bank", "account_name": "Golden Ray Energy Solutions Pvt Ltd", "account_number": "50200012345678", "ifsc": "hdfc0001234", "upi_id": "flarize@hdfcbank"}


@pytest.fixture
def admin(make_user):
    return make_user(grants={"company": ["view", "edit"]})


@pytest.fixture
def client(auth_client, admin):
    return auth_client(admin)


def detail(account, suffix=""):
    return f"{URL}{account.uid}/{suffix}"


class TestPermissions:
    def test_anonymous_is_401(self, api_client):
        account = BankAccountFactory()
        assert api_client.get(URL).status_code == 401
        assert api_client.post(detail(account, "make-primary/")).status_code == 401

    def test_view_only_cannot_write(self, auth_client, make_user):
        account = BankAccountFactory()
        client = auth_client(make_user(grants={"company": ["view"]}))
        assert client.get(URL).status_code == 200
        assert client.post(URL, NEW, format="json").status_code == 403
        assert client.patch(detail(account), {"label": "x"}, format="json").status_code == 403
        assert client.delete(detail(account)).status_code == 403
        assert client.post(detail(account, "make-primary/")).status_code == 403
        assert auth_client(make_user(grants={"settings": "*"})).get(URL).status_code == 403


class TestCreateAndRead:
    def test_first_account_becomes_primary_and_number_is_masked(self, client, admin):
        response = client.post(URL, NEW, format="json")
        assert response.status_code == 201, response.json()
        body = response.json()
        assert body["is_primary"] is True and body["ifsc"] == "HDFC0001234"
        assert body["account_number_last4"] == "5678" and body["account_number_masked"] == "XXXXXXXXXX5678"
        assert "50200012345678" not in response.content.decode()
        assert "50200012345678" not in client.get(URL).content.decode()
        entry = AuditLog.objects.get(action="company.bank_account_created")
        assert entry.after["account_number"] == "***" and entry.actor == admin

    def test_account_number_is_encrypted_at_rest(self, client):
        client.post(URL, NEW, format="json")
        with connection.cursor() as cursor:
            cursor.execute("SELECT account_number FROM company_bank_account")
            raw = cursor.fetchone()[0]
        assert raw.startswith("gAAAA") and "50200012345678" not in raw
        assert BankAccount.objects.get().account_number == "50200012345678"

    def test_creating_a_new_primary_moves_the_flag(self, client):
        old = BankAccountFactory(is_primary=True)
        body = client.post(URL, {**NEW, "is_primary": True}, format="json").json()
        assert body["is_primary"] is True and primary_account().uid.hex == body["uid"].replace("-", "")
        old.refresh_from_db()
        assert old.is_primary is False

    @pytest.mark.parametrize(
        "field,value",
        [("ifsc", "HDFC1234567"), ("account_number", "12AB"), ("account_number", "1234"), ("upi_id", "no-at-sign"), ("label", "")],
    )
    def test_validation(self, client, field, value):
        response = client.post(URL, {**NEW, field: value}, format="json")
        assert response.status_code == 400 and field in response.json()["errors"]

    def test_label_taken(self, client):
        BankAccountFactory(label="Current account")
        response = client.post(URL, NEW, format="json")
        assert response.status_code == 409 and response.json()["code"] == "label_taken"


class TestUpdateAndPrimary:
    def test_update_account_number(self, client):
        account = BankAccountFactory(account_number="111122223333")
        response = client.patch(detail(account), {"account_number": "999988887777", "expected_version": 1}, format="json")
        assert response.status_code == 200 and response.json()["account_number_last4"] == "7777" and response.json()["version"] == 2
        entry = AuditLog.objects.get(action="company.bank_account_updated")
        assert entry.before == {"account_number": "***"} and entry.after == {"account_number": "***"}

    def test_primary_flag_is_not_patchable(self, client):
        account = BankAccountFactory(is_primary=False)
        client.patch(detail(account), {"is_primary": True, "branch": "Kochi"}, format="json")
        account.refresh_from_db()
        assert account.is_primary is False and account.branch == "Kochi"

    def test_make_primary(self, client):
        first = BankAccountFactory(is_primary=True)
        second = BankAccountFactory()
        response = client.post(detail(second, "make-primary/"), {"expected_version": 1}, format="json")
        assert response.status_code == 200 and response.json()["is_primary"] is True
        first.refresh_from_db()
        assert first.is_primary is False and BankAccount.objects.filter(is_primary=True).count() == 1
        assert client.post(detail(second, "make-primary/")).json()["version"] == response.json()["version"]  # idempotent
        assert AuditLog.objects.filter(action="company.bank_account_made_primary").count() == 1

    def test_stale_version(self, client):
        account = BankAccountFactory(version=4)
        assert client.patch(detail(account), {"branch": "x", "expected_version": 3}, format="json").status_code == 409
        assert client.post(detail(account, "make-primary/"), {"expected_version": 3}, format="json").status_code == 409


class TestDelete:
    def test_primary_is_protected_while_others_exist(self, client):
        primary = BankAccountFactory(is_primary=True)
        other = BankAccountFactory()
        response = client.delete(detail(primary))
        assert response.status_code == 409 and response.json()["code"] == "primary_account_protected"
        assert client.delete(detail(other)).status_code == 204
        assert client.delete(detail(primary)).status_code == 204
        assert not BankAccount.objects.exists() and BankAccount.all_objects.count() == 2

    def test_deleted_label_can_be_reused(self, client):
        BankAccountFactory(label="Current account").soft_delete()
        assert client.post(URL, NEW, format="json").status_code == 201
