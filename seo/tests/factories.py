import factory

from seo.models import PageMetadata, Redirect


class PageMetadataFactory(factory.django.DjangoModelFactory):
    class Meta:
        model = PageMetadata

    page = factory.Sequence(lambda n: f"page-{n}")
    title = factory.Sequence(lambda n: f"Page {n} | Flarize")
    description = "Solar solutions for Kerala homes."
    keywords = factory.LazyFunction(lambda: ["solar", "kerala"])
    og_image_url = "/heroImg.png"


class RedirectFactory(factory.django.DjangoModelFactory):
    class Meta:
        model = Redirect

    from_path = factory.Sequence(lambda n: f"/old-{n}")
    to_path = factory.Sequence(lambda n: f"/new-{n}")
