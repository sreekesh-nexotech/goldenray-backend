from django.contrib.auth import password_validation
from django.contrib.auth.base_user import AbstractBaseUser, BaseUserManager
from django.db import models
from django.db.models import Q

from core.models import AllObjectsManager, BaseModel, BaseQuerySet, CIEmailField


class UserManager(BaseUserManager):
    """Live users only (soft-deleted users cannot authenticate). No ``create_superuser``: there is no superuser."""

    def get_queryset(self):
        return BaseQuerySet(self.model, using=self._db).filter(deleted_at__isnull=True)

    def create_user(self, email: str, password: str | None = None, *, role, **extra_fields):
        """Create a user. A raw ``password`` must pass ``AUTH_PASSWORD_VALIDATORS`` (the policy applies on every path
        that sets a password); without one the password is unusable and the account needs a set-password link."""
        if not email:
            raise ValueError("Users must have an e-mail address.")
        if role is None:
            raise ValueError("Users must have a role.")
        user = self.model(email=self.normalize_email(email).strip(), role=role, **extra_fields)
        if password:
            password_validation.validate_password(password, user)
            user.set_password(password)
        else:
            user.set_unusable_password()
        user.save(using=self._db)
        return user


class User(AbstractBaseUser, BaseModel):
    """Staff user. E-mail is the login (citext, unique among live users).

    ``is_staff`` exists only because Django's auth machinery expects it (always true). Authorization never reads
    ``is_staff`` and there is no ``is_superuser``: every permission comes from ``role`` via the registry. The link
    to an HR employee lives on ``hr_employee.user`` (DV-1).
    """

    email = CIEmailField(max_length=254)
    first_name = models.CharField(max_length=150, blank=True, default="")
    last_name = models.CharField(max_length=150, blank=True, default="")
    phone_e164 = models.CharField(max_length=16, blank=True, default="")
    is_active = models.BooleanField(default=True)
    is_staff = models.BooleanField(default=True)
    # Master data: PROTECT — a role cannot be deleted while users hold it.
    role = models.ForeignKey("accounts.Role", on_delete=models.PROTECT, related_name="users")
    title = models.CharField(max_length=64, blank=True, default="")
    last_login_at = models.DateTimeField(null=True, blank=True)
    password_changed_at = models.DateTimeField(null=True, blank=True)
    must_reset_password = models.BooleanField(default=False)

    last_login = None  # replaced by last_login_at (UPDATE_LAST_LOGIN is off; the login service stamps it)

    objects = UserManager()
    all_objects = AllObjectsManager()

    USERNAME_FIELD = "email"
    EMAIL_FIELD = "email"
    REQUIRED_FIELDS: list[str] = []

    class Meta:
        db_table = "accounts_user"
        default_manager_name = "objects"
        constraints = [
            models.UniqueConstraint(fields=["email"], condition=Q(deleted_at__isnull=True), name="accounts_user_email_uniq"),
            models.CheckConstraint(condition=Q(phone_e164="") | Q(phone_e164__regex=r"^\+[1-9][0-9]{6,14}$"), name="accounts_user_phone_e164_valid"),
        ]

    def __str__(self):
        return self.email

    def get_full_name(self) -> str:
        return f"{self.first_name} {self.last_name}".strip() or self.email

    def get_short_name(self) -> str:
        return self.first_name or self.email
