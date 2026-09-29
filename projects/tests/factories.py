import factory

from customers.tests.factories import CustomerFactory
from projects.models import Project


class ProjectFactory(factory.django.DjangoModelFactory):
    class Meta:
        model = Project

    number = factory.Sequence(lambda n: f"PROJ-T{n}")
    customer = factory.SubFactory(CustomerFactory)
    system_type = "ON_GRID"
    phase = "1P"
    size_kw = "3.00"
