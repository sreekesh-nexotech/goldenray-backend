from django.apps import AppConfig


class ProcurementConfig(AppConfig):
    name = "procurement"
    label = "procurement"
    verbose_name = "Procurement"

    def ready(self):
        from procurement.services import registrations

        registrations.register()
