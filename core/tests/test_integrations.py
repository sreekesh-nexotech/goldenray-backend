"""core.integrations: the platform's view of Admin-stored provider settings (resolver installed by the company app)."""

import pytest

from core import integrations


def test_without_a_resolver_nothing_is_stored(monkeypatch):
    monkeypatch.setattr(integrations, "_resolver", None)
    assert integrations.get_config("BUNNY") is None


def test_resolver_results_are_copied(monkeypatch):
    stored = {"host": "smtp.example.com"}
    monkeypatch.setattr(integrations, "_resolver", lambda key: stored)
    config = integrations.get_config("SMTP")
    config["host"] = "changed"
    assert stored["host"] == "smtp.example.com"


def test_register_and_unregister(monkeypatch):
    monkeypatch.setattr(integrations, "_resolver", None)
    resolver = integrations.register_resolver(lambda key: {"key": key})
    assert integrations.get_config("TWILIO") == {"key": "TWILIO"} and callable(resolver)
    integrations.unregister_resolver()
    assert integrations.get_config("TWILIO") is None


def test_unknown_key():
    with pytest.raises(ValueError):
        integrations.get_config("S3")
