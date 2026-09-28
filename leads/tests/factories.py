import datetime as dt
from decimal import Decimal

import factory
from django.utils import timezone

from leads.models import AffiliateApplication, CustomerInstallation, IssueType, KeralaDistrict, Lead, LeadNote, OtpRequest, Profession, WarrantyRequest


class LeadFactory(factory.django.DjangoModelFactory):
    class Meta:
        model = Lead

    number = factory.Sequence(lambda n: f"L-T{n}")
    kind = Lead.Kind.HOME_ENQUIRY
    form = Lead.Form.HOME_BOOKING
    name = factory.Sequence(lambda n: f"Lead {n}")
    phone_e164 = factory.Sequence(lambda n: f"+9197{n:08d}")


class LeadNoteFactory(factory.django.DjangoModelFactory):
    class Meta:
        model = LeadNote

    lead = factory.SubFactory(LeadFactory)
    body = "Called, no answer."


class AffiliateApplicationFactory(factory.django.DjangoModelFactory):
    class Meta:
        model = AffiliateApplication

    full_name = factory.Sequence(lambda n: f"Partner {n}")
    phone_e164 = factory.Sequence(lambda n: f"+9196{n:08d}")
    email = factory.Sequence(lambda n: f"partner{n}@example.com")
    profession = Profession.REAL_ESTATE_AGENT
    district = KeralaDistrict.ERNAKULAM


class WarrantyRequestFactory(factory.django.DjangoModelFactory):
    class Meta:
        model = WarrantyRequest

    full_name = factory.Sequence(lambda n: f"Owner {n}")
    phone_e164 = factory.Sequence(lambda n: f"+9195{n:08d}")
    issue_type = IssueType.INVERTER_FAULT
    description = "Inverter shows E02."


class CustomerInstallationFactory(factory.django.DjangoModelFactory):
    class Meta:
        model = CustomerInstallation

    customer_name = factory.Sequence(lambda n: f"Installed {n}")
    phone_e164 = factory.Sequence(lambda n: f"+9194{n:08d}")
    pincode = "682016"
    district = "ERNAKULAM"
    address = "MG Road"
    capacity_kw = Decimal("5.000")
    installed_on = dt.date(2025, 1, 10)
    status = CustomerInstallation.Status.COMPLETED


class OtpRequestFactory(factory.django.DjangoModelFactory):
    class Meta:
        model = OtpRequest

    phone_e164 = "+919876500001"
    provider_sid = factory.Sequence(lambda n: f"VE{n:032d}")
    expires_at = factory.LazyFunction(lambda: timezone.now() + dt.timedelta(minutes=10))
