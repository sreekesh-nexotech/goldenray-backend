from django.apps import AppConfig


class DevicesConfig(AppConfig):
    name = "devices"
    label = "devices"
    verbose_name = "Devices"

    def ready(self):
        from devices.services import providers

        # hr's device registries (mappings, reconciliation, dependencies, office summary), dashboard, ops report.
        providers.install()
