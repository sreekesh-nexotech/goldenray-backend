"""``timestamp`` (without time zone) columns for wall-clock values.

PLAN §2.9 stores ``attendance_raw_punch.device_time`` (the terminal's clock, as reported) and
``attendance_day.first_in`` / ``last_out`` (the office wall clock) as ``timestamp``: they are readings of a clock, not
instants. Django's ``DateTimeField`` under ``USE_TZ`` would turn a naive value into an instant of the server's zone
(and warn); this field keeps the value exactly as given and refuses an aware one (a programming error: convert with
the office zone first — ``engines.attendance.local_wall_clock``).
"""

from __future__ import annotations

import datetime

from django.db import models


class WallClockDateTimeField(models.DateTimeField):
    description = "Wall-clock date and time (timestamp without time zone)"

    def db_type(self, connection):
        return "timestamp"

    def get_prep_value(self, value):
        value = models.Field.get_prep_value(self, value)
        value = self.to_python(value)
        if isinstance(value, datetime.datetime) and value.tzinfo is not None:
            raise ValueError(f"{self.model.__name__}.{self.name} holds a wall-clock reading; pass a naive datetime.")
        return value

    def get_db_prep_value(self, value, connection, prepared=False):
        return value if prepared else self.get_prep_value(value)

    def pre_save(self, model_instance, add):
        return getattr(model_instance, self.attname)
