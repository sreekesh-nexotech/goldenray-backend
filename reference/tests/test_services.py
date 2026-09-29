"""Service-level paths of reference.services.lists / pincodes the HTTP tests do not reach."""

from decimal import Decimal

import pytest
from django.db import IntegrityError

from core.errors import Conflict, DomainError
from reference.services import lists, pincodes
from reference.tests import factories

pytestmark = pytest.mark.django_db


def test_integrity_error_mapping():
    assert isinstance(lists.integrity_error("wattage", "value", IntegrityError('violates unique constraint "reference_wattage_value_live_uniq"')), Conflict)
    other = lists.integrity_error("wattage", "value", IntegrityError('violates check constraint "reference_wattage_value_positive"'))
    assert not isinstance(other, Conflict) and other.code == "validation_error"


def test_check_violation_is_a_400(make_user):
    with pytest.raises(DomainError) as caught:
        lists.create_row(lists.WATTAGES, user=make_user(), data={"value": 0})
    assert caught.value.code == "validation_error" and caught.value.status == 400


def test_noop_update_keeps_the_version(make_user):
    row = factories.WattageFactory(value=10)
    assert lists.update_row(lists.WATTAGES, row, user=make_user(), data={"value": 10}).version == 1


def test_explicit_sort_order_is_kept(make_user):
    row = lists.create_row(lists.APPLIANCES, user=make_user(), data={"code": "fan", "name": "Fan", "watts": 60, "default_hours": Decimal("8"), "sort_order": 42})
    assert row.sort_order == 42


def test_pincode_update_of_offices_only_bumps_the_version(make_user):
    office = factories.PincodeOfficeFactory()
    pincode = office.pincode
    updated = pincodes.update_pincode(pincode, user=make_user(), data={"offices": [{"uid": office.uid, "office_name": office.office_name}]})
    assert updated.version == 2
    assert pincodes.update_pincode(updated, user=make_user(), data={}).version == 2


def test_pincode_integrity_error_on_update(make_user):
    factories.PincodeFactory(pincode="688011")
    other = factories.PincodeFactory(pincode="688012")
    with pytest.raises(Conflict) as caught:
        pincodes.update_pincode(other, user=make_user(), data={"pincode": "688011"})
    assert caught.value.code == "pincode_exists"


def test_pincode_defaults_come_from_the_first_office(make_user):
    created = pincodes.create_pincode(user=make_user(), data={"pincode": "686102", "district": "", "offices": [{"office_name": "A BO", "district": "KOTTAYAM", "state": "KERALA"}]})
    assert created.district == "KOTTAYAM" and created.state == "KERALA"
