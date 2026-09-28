"""Customer installations: staff CRUD (``leads/installations/``), the public showcase list (``installations``) and
the pincode stats (``installations/stats``, the legacy ``installation-stats``).

* Public list: live, COMPLETED, ``is_showcase`` rows only, filterable by pincode/district, newest first — never the
  customer's name, phone number or address.
* Stats: counts of COMPLETED installations for the pincode, for its district and for its district in the current
  (Asia/Kolkata) year — the legacy semantics (see :mod:`leads.services.pincode_directory`), computed in one
  aggregate query.
* Writes are versioned, audited and bump ``leads:installations`` (the public payloads' namespace). A new or moved
  installation takes its district from the pincode list when the staff user leaves it empty.
"""

from __future__ import annotations

from django.db import transaction
from django.db.models import Count, Q
from django.utils import timezone

from accounts.services.authz import can
from audit.services import changes, record, snapshot
from core.errors import PermissionDenied
from core.services import check_version, stamp_create
from flarize.cache_utils import bump
from leads.models import CustomerInstallation
from leads.services import pincode_directory

CACHE_NAMESPACE = "leads:installations"
FIELDS = ("customer_name", "phone_e164", "pincode", "district", "address", "capacity_kw", "system_type", "installed_on", "status", "is_showcase", "photo", "assignee")
COMPLETED = CustomerInstallation.Status.COMPLETED


def installations_queryset():
    return CustomerInstallation.objects.select_related("photo", "assignee")


def showcase_queryset(*, pincode: str | None = None, district: str | None = None):
    queryset = CustomerInstallation.objects.filter(is_showcase=True, status=COMPLETED).select_related("photo").order_by("-installed_on", "-id")
    if pincode:
        queryset = queryset.filter(pincode=pincode)
    if district:
        queryset = queryset.filter(district__iexact=district)
    return queryset


def installation_stats(pincode: str, *, today=None) -> dict:
    today = today or timezone.localdate()
    directory = pincode_directory.current()
    district = directory.district_for(pincode) or pincode_directory.legacy_fallback(pincode)
    listed = directory.pincodes_in(district)
    in_district = Q(pincode__in=sorted(listed)) if listed is not None else Q(district__iexact=district)
    counts = CustomerInstallation.objects.filter(status=COMPLETED).aggregate(
        in_pincode=Count("id", filter=Q(pincode=pincode)),
        in_district=Count("id", filter=in_district),
        in_year=Count("id", filter=in_district & Q(installed_on__year=today.year)),
    )
    return {
        "pincode": pincode,
        "district": district,
        "pincode_installations": counts["in_pincode"],
        "district_installations": counts["in_district"],
        "current_year_installations": counts["in_year"],
        "year": today.year,
    }


def _check_assignee(user, assignee, current=None) -> None:
    """Assigning an installation to someone other than yourself needs ``leads.manage`` (as for leads)."""
    if assignee == current or (assignee is not None and assignee.pk == getattr(user, "pk", None)):
        return
    if not can(user, "leads", "manage"):
        raise PermissionDenied("assign_forbidden", "Assigning to someone else needs the leads manage permission.", errors={"assignee_uid": ["Not allowed."]})


def _with_district(values: dict) -> dict:
    if values.get("pincode") and not values.get("district"):
        values["district"] = pincode_directory.current().district_for(values["pincode"]) or ""
    return values


@transaction.atomic
def create_installation(*, user, data: dict) -> CustomerInstallation:
    values = _with_district({name: data[name] for name in FIELDS if name in data and data[name] is not None})
    if "assignee" in data:
        _check_assignee(user, data["assignee"])
    installation = CustomerInstallation(**values)
    stamp_create(installation, user)
    installation.save()
    record("leads.installation_created", obj=installation, actor=user, after=snapshot(installation, FIELDS))
    bump(CACHE_NAMESPACE)
    return installation


@transaction.atomic
def update_installation(instance: CustomerInstallation, *, user, data: dict, expected_version=None) -> CustomerInstallation:
    installation = CustomerInstallation.objects.select_for_update().get(pk=instance.pk)
    check_version(installation, expected_version)
    values = {name: data[name] for name in FIELDS if name in data and data[name] != getattr(installation, name)}
    if "assignee" in values:
        _check_assignee(user, values["assignee"], installation.assignee)
    if "pincode" in values and "district" not in data:
        values["district"] = pincode_directory.current().district_for(values["pincode"]) or ""
    if not values:
        return installation
    before = snapshot(installation, FIELDS)
    installation.versioned_update(user, **values)
    changed_before, changed_after = changes(before, snapshot(installation, FIELDS))
    record("leads.installation_updated", obj=installation, actor=user, before=changed_before, after=changed_after)
    bump(CACHE_NAMESPACE)
    return installation


@transaction.atomic
def delete_installation(instance: CustomerInstallation, *, user, expected_version=None) -> None:
    installation = CustomerInstallation.objects.select_for_update().get(pk=instance.pk)
    check_version(installation, expected_version)
    installation.soft_delete(user)
    record("leads.installation_deleted", obj=installation, actor=user, before=snapshot(installation, FIELDS))
    bump(CACHE_NAMESPACE)
