"""``hr_employee`` (PLAN §2.9): a person whose attendance is tracked. The Studio login link lives here (DV-1).

* ``user`` is the optional Studio login (OneToOne, DV-1: accounts must not import hr). Linking assigns the Staff role,
  unlinking a Staff-only login deactivates it (docs/decisions/hr.md); deactivating the employee emits
  ``hr.employee_deactivated`` and accounts deactivates the login.
* ``identity_method`` is how HR says the person identifies at the terminal; it is declared, never derived (on the
  eSSL firmware a face and a card punch look identical).
* ``photo`` is a PRIVATE media asset in the reserved ``hr/employees`` folder, served through signed URLs only.
"""

from __future__ import annotations

from django.conf import settings
from django.db import models
from django.db.models import F, Q
from django.db.models.functions import Upper

from core.models import BaseModel

PHONE_E164_REGEX = r"^\+[1-9][0-9]{6,14}$"


class Employee(BaseModel):
    class IdentityMethod(models.TextChoices):
        UNSPECIFIED = "UNSPECIFIED", "Not stated"
        FACE = "FACE", "Face"
        CARD = "CARD", "Card"
        FACE_CARD = "FACE_CARD", "Face or card"

    code = models.CharField(max_length=50, help_text="Employee code (unique among live employees, case-insensitive).")
    full_name = models.CharField(max_length=150)
    # Master data for the person, not owned by them: SET_NULL keeps the employee (and history) if the office goes.
    office = models.ForeignKey("hr.Office", null=True, blank=True, on_delete=models.SET_NULL, related_name="employees")
    # SET_NULL: without a shift of their own the office default applies.
    shift = models.ForeignKey("hr.Shift", null=True, blank=True, on_delete=models.SET_NULL, related_name="employees")
    department = models.CharField(max_length=100, blank=True, default="")
    designation = models.CharField(max_length=100, blank=True, default="")
    email = models.EmailField(max_length=254, blank=True, default="", help_text="Contact address; not unique and not the login.")
    phone_e164 = models.CharField(max_length=16, blank=True, default="")
    joined_on = models.DateField(null=True, blank=True)
    left_on = models.DateField(null=True, blank=True)
    identity_method = models.CharField(max_length=12, choices=IdentityMethod.choices, default=IdentityMethod.UNSPECIFIED)
    # Attribution-like reference to a file: SET_NULL — the media usage guard refuses deleting a referenced asset.
    photo = models.ForeignKey("media.MediaAsset", null=True, blank=True, on_delete=models.SET_NULL, related_name="+")
    is_active = models.BooleanField(default=True)
    # DV-1: the login link lives on the employee. SET_NULL: deleting an account never deletes the employee.
    user = models.OneToOneField(settings.AUTH_USER_MODEL, null=True, blank=True, on_delete=models.SET_NULL, related_name="employee")

    class Meta:
        db_table = "hr_employee"
        ordering = ["full_name", "id"]
        constraints = [
            models.UniqueConstraint(Upper("code"), condition=Q(deleted_at__isnull=True), name="hr_employee_code_live_uniq"),
            models.CheckConstraint(condition=~Q(code=""), name="hr_employee_code_not_blank"),
            models.CheckConstraint(condition=Q(identity_method__in=["UNSPECIFIED", "FACE", "CARD", "FACE_CARD"]), name="hr_employee_identity_method_valid"),
            models.CheckConstraint(condition=Q(phone_e164="") | Q(phone_e164__regex=PHONE_E164_REGEX), name="hr_employee_phone_e164_valid"),
            models.CheckConstraint(condition=Q(left_on__isnull=True) | Q(joined_on__isnull=True) | Q(left_on__gte=F("joined_on")), name="hr_employee_left_after_joined"),
        ]
        indexes = [models.Index(fields=["office", "is_active"], name="hr_employee_office_active_idx")]

    def __str__(self) -> str:
        return f"{self.code} {self.full_name}"

    @property
    def effective_shift(self):
        """The employee's own shift, else the office default (never a hard-coded one)."""
        if self.shift_id is not None:
            return self.shift
        office = self.office
        return office.default_shift if office is not None else None
