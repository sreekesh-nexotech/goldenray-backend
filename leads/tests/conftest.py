"""Leads test fixtures: a clean fake Twilio outbox, a pincode directory, a verified phone helper."""

import json
from pathlib import Path

import pytest

from leads.services import pincode_directory, twilio_verify

FIXTURES = Path(__file__).resolve().parent / "fixtures" / "legacy_backend"


def load_fixture(name: str):
    return json.loads((FIXTURES / name).read_text())


@pytest.fixture(autouse=True)
def _fake_twilio_outbox():
    twilio_verify.SENT.clear()
    yield
    twilio_verify.SENT.clear()


@pytest.fixture
def legacy_pincodes():
    """The legacy ``pincodes`` table (distinct pincode/district, first id) as the stats' pincode directory."""
    previous = pincode_directory.register(pincode_directory.TableDirectory(load_fixture("pincodes.json")))
    yield pincode_directory.current()
    pincode_directory.register(previous)


@pytest.fixture
def verified(api_client):
    """``verified(phone) -> token``: runs the real otp/send + otp/verify flow (fake backend, code 000000)."""

    def _verified(phone="9876543210"):
        assert api_client.post("/api/public/v1/otp/send/", {"phone": phone}, format="json").status_code == 200
        response = api_client.post("/api/public/v1/otp/verify/", {"phone": phone, "code": twilio_verify.FAKE_APPROVED_CODE}, format="json")
        assert response.status_code == 200, response.json()
        return response.json()["verification_token"]

    return _verified
