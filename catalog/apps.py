from django.apps import AppConfig


class CatalogConfig(AppConfig):
    name = "catalog"
    label = "catalog"
    verbose_name = "Catalog"

    def ready(self):
        from catalog.models import Brand, Component
        from catalog.services import dashboard, usage  # noqa: F401 - usage registers the catalog.replacements provider
        from media import usage as media_usage

        dashboard.register()
        media_usage.register(Brand, "logo")
        media_usage.register(Component, "datasheet")
        media_usage.register(Component, "primary_image")
