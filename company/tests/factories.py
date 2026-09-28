import factory

from company.models import BankAccount, CompanyProfile, Integration


class CompanyProfileFactory(factory.django.DjangoModelFactory):
    class Meta:
        model = CompanyProfile

    legal_name = "Golden Ray Energy Solutions Pvt Ltd"
    trade_name = "Flarize"
    email = "hello@flarize.com"
    phone_e164 = "+914842000000"
    address_line = "2nd Floor, MG Road"
    address_locality = "Kochi"
    address_region = "Kerala"
    postal_code = "682016"
    gstin = "32AABCG1234F1Z5"


class BankAccountFactory(factory.django.DjangoModelFactory):
    class Meta:
        model = BankAccount

    label = factory.Sequence(lambda n: f"Account {n}")
    bank = "State Bank of India"
    account_name = "Golden Ray Energy Solutions Pvt Ltd"
    account_number = factory.Sequence(lambda n: f"{30000000000 + n}")
    ifsc = "SBIN0001234"


class IntegrationFactory(factory.django.DjangoModelFactory):
    class Meta:
        model = Integration

    key = Integration.Key.SMTP
    is_enabled = True
    config = factory.LazyFunction(
        lambda: {"host": "smtp.example.com", "port": 587, "username": "mailer", "password": "smtp-secret-value", "use_tls": True, "use_ssl": False, "from_email": "Flarize <no-reply@flarize.com>"}
    )
