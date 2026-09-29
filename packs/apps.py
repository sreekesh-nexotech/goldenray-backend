from django.apps import AppConfig


class PacksConfig(AppConfig):
    name = "packs"
    label = "packs"
    verbose_name = "Packs"

    def ready(self):
        from packs.services import registrations

        registrations.register()
