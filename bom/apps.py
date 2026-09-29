from django.apps import AppConfig


class BomConfig(AppConfig):
    name = "bom"
    label = "bom"
    verbose_name = "BOM"

    def ready(self):
        from bom.services import registrations

        registrations.register()
