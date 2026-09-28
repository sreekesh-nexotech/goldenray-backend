"""Vocabularies shared by the leads models. Labels are the legacy website's values, so old payloads map 1:1."""

from __future__ import annotations

from django.db import models


class KeralaDistrict(models.TextChoices):
    THIRUVANANTHAPURAM = "Thiruvananthapuram", "Thiruvananthapuram"
    KOLLAM = "Kollam", "Kollam"
    PATHANAMTHITTA = "Pathanamthitta", "Pathanamthitta"
    ALAPPUZHA = "Alappuzha", "Alappuzha"
    KOTTAYAM = "Kottayam", "Kottayam"
    IDUKKI = "Idukki", "Idukki"
    ERNAKULAM = "Ernakulam", "Ernakulam"
    THRISSUR = "Thrissur", "Thrissur"
    PALAKKAD = "Palakkad", "Palakkad"
    MALAPPURAM = "Malappuram", "Malappuram"
    KOZHIKODE = "Kozhikode", "Kozhikode"
    WAYANAD = "Wayanad", "Wayanad"
    KANNUR = "Kannur", "Kannur"
    KASARAGOD = "Kasaragod", "Kasaragod"


def by_label(choices: type[models.TextChoices]) -> dict[str, str]:
    """``{legacy label or value, case-insensitive: value}`` — how old payloads and imports reach the codes."""
    mapping = {}
    for value, label in choices.choices:
        mapping[str(label).casefold()] = value
        mapping[str(value).casefold()] = value
    return mapping
