"""E.164 normalisation (region IN) — the one phone rule of the sales context."""

import pytest

from customers.services.phones import InvalidPhone, national_digits, normalise_phone, try_normalise


@pytest.mark.parametrize(
    "value",
    ["9876543210", "09876543210", "+91 98765-43210", "919876543210", "0091 9876543210", "(98765) 43210", "98765.43210", " +919876543210 "],
)
def test_indian_mobile_spellings(value):
    assert normalise_phone(value, mobile_only=True, regions=frozenset({"IN"})) == "+919876543210"


def test_staff_may_enter_landlines_and_foreign_numbers():
    assert normalise_phone("0484 2000000") == "+914842000000"
    assert normalise_phone("+1 415 555 0100") == "+14155550100"


@pytest.mark.parametrize(
    ("value", "kwargs", "message"),
    [
        ("", {}, "Enter a phone number."),
        ("12345", {}, "Enter a valid phone number."),
        ("abc", {}, "Enter a valid phone number."),
        ("9" * 40, {}, "Enter a valid phone number."),
        ("5876543210", {"mobile_only": True}, "Enter a valid 10-digit Indian mobile number."),
        ("12345678901", {"mobile_only": True}, "Enter a valid 10-digit Indian mobile number."),
        ("+14155550100", {"mobile_only": True, "regions": frozenset({"IN"})}, "Only Indian numbers are accepted."),
    ],
)
def test_invalid(value, kwargs, message):
    with pytest.raises(InvalidPhone) as caught:
        normalise_phone(value, **kwargs)
    assert str(caught.value) == message
    assert try_normalise(value, **kwargs) is None


@pytest.mark.parametrize("value", ["+9876543210", "98765+43210", "+ 98765 43210", "+(98765) 43210"])
def test_legacy_plus_spellings_of_an_indian_mobile(value):
    """The legacy website rule dropped every ``+`` (then took 10 digits, or 91 + 10 digits): ``+9876543210`` was stored as
    ``9876543210``. A spelling that parses as no valid number is read that way too (never a valid foreign number)."""
    assert normalise_phone(value, mobile_only=True, regions=frozenset({"IN"})) == "+919876543210"
    assert normalise_phone(value) == "+919876543210"


def test_a_valid_foreign_number_is_not_reinterpreted_as_indian():
    with pytest.raises(InvalidPhone) as caught:
        normalise_phone("+65 9123 4567", mobile_only=True, regions=frozenset({"IN"}))  # Singapore: ten digits once the + is gone
    assert str(caught.value) == "Only Indian numbers are accepted."
    assert normalise_phone("+65 9123 4567") == "+6591234567"


def test_national_digits():
    assert national_digits("+919876543210") == "9876543210"
    assert national_digits("") == "" and national_digits("junk") == "junk"
