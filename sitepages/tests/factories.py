import factory

from sitepages.models import Page, PageImageSlot, PageSeo, PageTextSlot


class PageFactory(factory.django.DjangoModelFactory):
    """A test page; routes/slugs never collide with the registry's real routes."""

    class Meta:
        model = Page

    slug = factory.Sequence(lambda n: f"test-page-{n}")
    route = factory.LazyAttribute(lambda page: f"/test/{page.slug}")
    title = factory.Sequence(lambda n: f"Test page {n}")
    group = "Main"
    status = Page.Status.PUBLISHED
    sort_order = factory.Sequence(lambda n: n)


class PageSeoFactory(factory.django.DjangoModelFactory):
    class Meta:
        model = PageSeo

    page = factory.SubFactory(PageFactory)
    seo_title = "A page about solar"
    meta_description = "Rooftop solar for Kerala homes, designed and installed by Flarize with the subsidy paperwork handled."


class PageTextSlotFactory(factory.django.DjangoModelFactory):
    class Meta:
        model = PageTextSlot

    page = factory.SubFactory(PageFactory)
    key = factory.Sequence(lambda n: f"text_{n}")
    label = factory.Sequence(lambda n: f"Text {n}")
    kind = PageTextSlot.Kind.SHORT_TEXT
    max_length = 90
    sort_order = factory.Sequence(lambda n: n)


class PageImageSlotFactory(factory.django.DjangoModelFactory):
    class Meta:
        model = PageImageSlot

    page = factory.SubFactory(PageFactory)
    key = factory.Sequence(lambda n: f"image_{n}")
    label = factory.Sequence(lambda n: f"Image {n}")
    sort_order = factory.Sequence(lambda n: n)
