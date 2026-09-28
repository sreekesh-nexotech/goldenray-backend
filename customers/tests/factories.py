import factory

from customers.models import Customer, CustomerNote


class CustomerFactory(factory.django.DjangoModelFactory):
    class Meta:
        model = Customer

    code = factory.Sequence(lambda n: f"CUST-T{n:05d}")
    name = factory.Sequence(lambda n: f"Customer {n}")
    phone_e164 = factory.Sequence(lambda n: f"+9198{n:08d}")
    source = Customer.Source.SALES_ENTRY


class CustomerNoteFactory(factory.django.DjangoModelFactory):
    class Meta:
        model = CustomerNote

    customer = factory.SubFactory(CustomerFactory)
    body = factory.Sequence(lambda n: f"Note {n}")
