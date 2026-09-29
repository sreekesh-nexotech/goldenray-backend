from django.apps import AppConfig


class SitepagesConfig(AppConfig):
    name = "sitepages"
    label = "sitepages"
    verbose_name = "Site pages"

    def ready(self):
        from core import dashboard
        from media import usage
        from sitepages.models import PageImageSlot, PageSeo
        from sitepages.services.pages import dashboard_counts

        # media.usage refuses deleting an asset while a live slot or SEO block shows it on the website.
        usage.register(PageImageSlot, "asset")
        usage.register(PageSeo, "og_image")
        dashboard.register("pages")(dashboard_counts)
