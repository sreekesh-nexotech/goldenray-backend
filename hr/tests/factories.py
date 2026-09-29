import datetime as dt

import factory

from hr.models import AttendanceRule, Employee, Holiday, LeaveRecord, LeaveType, Office, Shift


class ShiftFactory(factory.django.DjangoModelFactory):
    class Meta:
        model = Shift

    code = factory.Sequence(lambda n: f"S{n}")
    name = factory.Sequence(lambda n: f"Shift {n}")
    start_time = dt.time(9, 30)
    end_time = dt.time(18, 30)


class OfficeFactory(factory.django.DjangoModelFactory):
    class Meta:
        model = Office

    code = factory.Sequence(lambda n: f"OF{n}")
    name = factory.Sequence(lambda n: f"Office {n}")
    timezone = "Asia/Kolkata"


class EmployeeFactory(factory.django.DjangoModelFactory):
    class Meta:
        model = Employee

    code = factory.Sequence(lambda n: f"E{n:04d}")
    full_name = factory.Faker("name")
    office = factory.SubFactory(OfficeFactory)


class HolidayFactory(factory.django.DjangoModelFactory):
    class Meta:
        model = Holiday

    date = factory.Sequence(lambda n: dt.date(2026, 1, 1) + dt.timedelta(days=n))
    name = factory.Sequence(lambda n: f"Holiday {n}")


class LeaveTypeFactory(factory.django.DjangoModelFactory):
    class Meta:
        model = LeaveType

    code = factory.Sequence(lambda n: f"LT{n}")
    name = factory.Sequence(lambda n: f"Leave type {n}")


class LeaveRecordFactory(factory.django.DjangoModelFactory):
    class Meta:
        model = LeaveRecord

    employee = factory.SubFactory(EmployeeFactory)
    leave_type = factory.SubFactory(LeaveTypeFactory)
    date_from = dt.date(2026, 3, 2)
    date_to = dt.date(2026, 3, 3)


class AttendanceRuleFactory(factory.django.DjangoModelFactory):
    class Meta:
        model = AttendanceRule

    name = factory.Sequence(lambda n: f"Rule {n}")
    rules = factory.LazyFunction(lambda: {"half_day_after_minutes": 45})
