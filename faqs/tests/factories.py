import factory
from django.utils import timezone

from faqs.models import Faq, FaqCategory
from sitepages.tests.factories import PageFactory


class FaqCategoryFactory(factory.django.DjangoModelFactory):
    class Meta:
        model = FaqCategory

    name = factory.Sequence(lambda n: f"Category {n}")
    slug = factory.Sequence(lambda n: f"category-{n}")
    sort_order = factory.Sequence(lambda n: n)


class FaqFactory(factory.django.DjangoModelFactory):
    class Meta:
        model = Faq

    question = factory.Sequence(lambda n: f"Question {n}?")
    answer = factory.Sequence(lambda n: f"Answer {n}.")
    page = factory.SubFactory(PageFactory)
    section = ""
    sort_order = factory.Sequence(lambda n: n)
    status = Faq.Status.DRAFT


class PublishedFaqFactory(FaqFactory):
    status = Faq.Status.PUBLISHED
    published_at = factory.LazyFunction(timezone.now)
