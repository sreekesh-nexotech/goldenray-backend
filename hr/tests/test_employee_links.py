"""link-user / unlink-user (Staff role automation, DV-1) and the private employee photo."""

import pytest
from django.core import mail
from django.core.files.uploadedfile import SimpleUploadedFile

from accounts.models import User
from accounts.tests.factories import seeded_role, super_admin_role
from audit.models import AuditLog
from hr.models import Employee
from hr.tests.factories import EmployeeFactory
from media import folders
from media.models import MediaAsset
from media.tests.files import image_bytes, pdf

pytestmark = pytest.mark.django_db
URL = "/api/v1/hr/employees/"


def detail(employee, suffix=""):
    return f"{URL}{employee.uid}/{suffix}"


def link(client, employee, **body):
    return client.post(detail(employee, "link-user/"), body, format="json")


class TestLinkExistingAccount:
    def test_a_role_within_staff_becomes_staff(self, hr_client, make_user):
        account = make_user(grants={"leave": ["view"]}, scopes={"leave": "self"})
        employee = EmployeeFactory()
        response = link(hr_client, employee, user_uid=str(account.uid), expected_version=1)
        assert response.status_code == 200, response.json()
        body = response.json()
        assert body["staff_role_assigned"] is True and body["account_created"] is False
        assert body["employee"]["user"]["uid"] == str(account.uid) and body["employee"]["user"]["role"]["slug"] == "staff"
        account.refresh_from_db()
        assert account.role.slug == "staff" and Employee.objects.get(pk=employee.pk).user_id == account.pk
        assert {"hr.staff_role_assigned", "hr.employee_user_linked"} <= set(AuditLog.objects.values_list("action", flat=True))

    def test_a_wider_role_is_kept(self, hr_client, make_user):
        account = make_user(grants={"employees": ["view"], "leave": ["view"]}, scopes={"employees": "all", "leave": "all"})
        role = account.role
        body = link(hr_client, EmployeeFactory(), user_uid=str(account.uid)).json()
        assert body["staff_role_assigned"] is False
        account.refresh_from_db()
        assert account.role == role

    def test_the_staff_login_then_sees_its_own_leave_only(self, hr_client, make_user, auth_client):
        account = make_user(grants={"leave": ["view"]}, scopes={"leave": "self"})
        employee = EmployeeFactory()
        link(hr_client, employee, user_uid=str(account.uid))
        client = auth_client(User.objects.get(pk=account.pk))
        assert client.get("/api/v1/hr/leave/").status_code == 200
        assert client.get("/api/v1/dashboard/").status_code == 200

    def test_a_login_the_caller_could_not_manage_is_refused(self, hr_client, make_user):
        response = link(hr_client, EmployeeFactory(), user_uid=str(make_user(grants={"leads": ["view"]}).uid))
        assert response.status_code == 403 and response.json()["code"] == "user_exceeds_own_grants"

    def test_guards(self, hr_client, make_user):
        account = make_user(grants={"leave": ["view"]})
        EmployeeFactory(user=account)
        response = link(hr_client, EmployeeFactory(), user_uid=str(account.uid))
        assert response.status_code == 409 and response.json()["code"] == "user_already_linked"
        linked = EmployeeFactory(user=make_user())
        response = link(hr_client, linked, user_uid=str(make_user().uid))
        assert response.status_code == 409 and response.json()["code"] == "employee_already_linked"
        response = link(hr_client, EmployeeFactory(is_active=False), user_uid=str(make_user().uid))
        assert response.status_code == 409 and response.json()["code"] == "employee_inactive"
        response = link(hr_client, EmployeeFactory(), user_uid="00000000-0000-0000-0000-000000000000")
        assert response.status_code == 404 and response.json()["code"] == "user_not_found"
        response = link(hr_client, EmployeeFactory(), user_uid=str(make_user(role=super_admin_role()).uid))
        assert response.status_code == 403 and response.json()["code"] == "super_admin_required"
        response = link(hr_client, EmployeeFactory(), user_uid=str(make_user(grants={"pricing": "*"}).uid))
        assert response.status_code == 403 and response.json()["code"] == "user_exceeds_own_grants"
        response = link(hr_client, EmployeeFactory(), user_uid=str(make_user(grants={"employees": ["view"]}, is_active=False).uid))
        assert response.status_code == 409 and response.json()["code"] == "user_inactive"

    def test_relinking_the_same_login_is_idempotent(self, hr_client, make_user):
        account = make_user(role=seeded_role("staff"))
        employee = EmployeeFactory(user=account)
        body = link(hr_client, employee, user_uid=str(account.uid)).json()
        assert body["employee"]["version"] == 1 and body["staff_role_assigned"] is False

    def test_linking_yourself_keeps_your_role(self, hr_client, hr_user):
        own = EmployeeFactory()
        body = link(hr_client, own, user_uid=str(hr_user.uid)).json()
        assert body["staff_role_assigned"] is False and body["employee"]["user"]["uid"] == str(hr_user.uid)

    def test_nobody_turns_their_own_role_into_staff(self, auth_client, make_user):
        # a role within Staff's grants that can still link (employees.edit is outside Staff, so give it separately)
        user = make_user(grants={"employees": ["view", "edit"]}, scopes={"employees": "all"})
        response = link(auth_client(user), EmployeeFactory(), user_uid=str(user.uid))
        assert response.status_code == 200 and response.json()["staff_role_assigned"] is False

    def test_a_retired_staff_login_is_reactivated(self, hr_client, make_user):
        account = make_user(role=seeded_role("staff"), is_active=False)
        body = link(hr_client, EmployeeFactory(), user_uid=str(account.uid)).json()
        assert body["account_reactivated"] is True and body["employee"]["user"]["is_active"] is True

    def test_body_must_name_one_account(self, hr_client):
        employee = EmployeeFactory()
        assert link(hr_client, employee).status_code == 400
        response = link(hr_client, employee, user_uid="00000000-0000-0000-0000-000000000001", email="x@example.com")
        assert response.status_code == 400 and response.json()["code"] == "validation_error"


class TestLinkNewAccount:
    def test_creates_a_staff_account_with_an_invitation(self, hr_client, django_capture_on_commit_callbacks):
        employee = EmployeeFactory(full_name="Asha Menon Kurian")
        with django_capture_on_commit_callbacks(execute=True):
            response = link(hr_client, employee, email="asha@example.com")
        assert response.status_code == 200, response.json()
        body = response.json()
        assert body["account_created"] is True and body["staff_role_assigned"] is True
        account = User.objects.get(email="asha@example.com")
        assert account.role.slug == "staff" and account.first_name == "Asha" and account.last_name == "Menon Kurian"
        assert account.must_reset_password and not account.has_usable_password()
        assert len(mail.outbox) == 1 and mail.outbox[0].to == ["asha@example.com"]

    def test_email_taken(self, hr_client, make_user):
        make_user(email="taken@example.com")
        response = link(hr_client, EmployeeFactory(), email="taken@example.com")
        assert response.status_code == 409 and response.json()["code"] == "email_taken"

    def test_creating_staff_accounts_needs_the_staff_grants(self, auth_client, make_user):
        client = auth_client(make_user(grants={"employees": ["view", "edit"]}, scopes={"employees": "all"}))
        response = link(client, EmployeeFactory(), email="new@example.com")
        assert response.status_code == 403 and response.json()["code"] == "role_exceeds_own_grants"


class TestUnlink:
    def test_a_staff_login_is_deactivated(self, hr_client, make_user):
        account = make_user(role=seeded_role("staff"))
        employee = EmployeeFactory(user=account)
        response = hr_client.post(detail(employee, "unlink-user/"), {"note": "Left", "expected_version": 1}, format="json")
        assert response.status_code == 200, response.json()
        body = response.json()
        assert body["account_deactivated"] is True and body["employee"]["user"] is None
        account.refresh_from_db()
        assert account.is_active is False
        assert AuditLog.objects.get(action="hr.employee_user_unlinked").note == "Left"

    def test_another_role_is_kept_active(self, hr_client, make_user):
        account = make_user(grants={"employees": ["view"]}, scopes={"employees": "all"})
        employee = EmployeeFactory(user=account)
        assert hr_client.post(detail(employee, "unlink-user/"), {}, format="json").json()["account_deactivated"] is False
        account.refresh_from_db()
        assert account.is_active is True

    def test_guards(self, hr_client, auth_client, hr_user, make_user):
        response = hr_client.post(detail(EmployeeFactory(), "unlink-user/"), {}, format="json")
        assert response.status_code == 409 and response.json()["code"] == "employee_not_linked"
        own = EmployeeFactory(user=hr_user)
        assert hr_client.post(detail(own, "unlink-user/"), {}, format="json").json()["code"] == "self_action_denied"
        boss = EmployeeFactory(user=make_user(role=super_admin_role()))
        assert hr_client.post(detail(boss, "unlink-user/"), {}, format="json").json()["code"] == "super_admin_required"
        employee = EmployeeFactory(user=make_user(), version=2)
        assert hr_client.post(detail(employee, "unlink-user/"), {"expected_version": 1}, format="json").json()["code"] == "stale_version"


class TestPhoto:
    def upload(self, client, employee, data=None, **extra):
        file = SimpleUploadedFile("face.jpg", data or image_bytes("JPEG"), content_type="image/jpeg")
        return client.post(detail(employee, "photo/"), {"file": file, **extra}, format="multipart")

    def test_upload_is_private_and_signed(self, hr_client):
        employee = EmployeeFactory()
        response = self.upload(hr_client, employee, expected_version=1)
        assert response.status_code == 200, response.json()
        photo = response.json()["photo"]
        asset = MediaAsset.objects.get(uid=photo["uid"])
        assert asset.visibility == "PRIVATE" and asset.kind == "PHOTO" and asset.folder == "hr/employees" and folders.is_reserved(asset.folder)
        assert "/api/v1/media/download/" in photo["url"] and photo["expires_at"]
        assert hr_client.get(photo["url"]).status_code == 200
        assert hr_client.get("/api/v1/media/").json()["count"] == 0  # hidden from the media library

    def test_replacing_deletes_the_old_file(self, hr_client):
        employee = EmployeeFactory()
        first = self.upload(hr_client, employee).json()["photo"]["uid"]
        second = self.upload(hr_client, employee).json()["photo"]["uid"]
        assert first != second and MediaAsset.all_objects.get(uid=first).deleted_at is not None
        assert MediaAsset.objects.get(uid=second).deleted_at is None

    def test_wrong_type_and_stale_version(self, hr_client):
        employee = EmployeeFactory(version=3)
        response = self.upload(hr_client, employee, data=pdf())
        assert response.status_code == 400
        response = self.upload(hr_client, employee, expected_version=2)
        assert response.status_code == 409 and response.json()["code"] == "stale_version"
        assert MediaAsset.objects.filter(kind="PHOTO").count() == 0  # the stored upload was removed again

    def test_remove(self, hr_client):
        employee = EmployeeFactory()
        uid = self.upload(hr_client, employee).json()["photo"]["uid"]
        response = hr_client.delete(detail(employee, "photo/"))
        assert response.status_code == 200 and response.json()["photo"] is None
        assert MediaAsset.all_objects.get(uid=uid).deleted_at is not None
        response = hr_client.delete(detail(employee, "photo/"))
        assert response.status_code == 404 and response.json()["code"] == "no_photo"

    def test_needs_edit(self, auth_client, make_user):
        client = auth_client(make_user(grants={"employees": ["view"]}, scopes={"employees": "all"}))
        assert self.upload(client, EmployeeFactory()).status_code == 403

    def test_the_media_library_cannot_delete_a_used_photo(self, hr_client, auth_client, make_user):
        employee = EmployeeFactory()
        uid = self.upload(hr_client, employee).json()["photo"]["uid"]
        from media.services import assets

        with pytest.raises(Exception) as error:
            assets.delete_asset(MediaAsset.objects.get(uid=uid), user=None)
        assert getattr(error.value, "code", "") == "media_in_use"
