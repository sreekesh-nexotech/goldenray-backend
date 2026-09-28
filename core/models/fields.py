"""Shared model fields."""

from django.db import models


class CIEmailField(models.EmailField):
    """E-mail stored as Postgres ``citext``: equality and unique constraints are case-insensitive.

    The ``citext`` extension is created by ``accounts.0001_initial`` (the first migration that needs it).
    """

    description = "Case-insensitive e-mail address (citext)"

    def db_type(self, connection):
        return "citext"
