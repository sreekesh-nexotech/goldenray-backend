from django.apps import AppConfig


class SeoConfig(AppConfig):
    name = "seo"
    label = "seo"
    verbose_name = "SEO"

    def ready(self):
        from media import usage
        from seo.models import PageMetadata

        # media.usage refuses deleting an asset a live page-metadata row shows as its Open Graph image.
        usage.register(PageMetadata, "og_image")
