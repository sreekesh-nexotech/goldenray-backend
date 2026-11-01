from django.apps import AppConfig


class ProcurementConfig(AppConfig):
    name = "procurement"
    label = "procurement"
    verbose_name = "Procurement"

    def ready(self):
        from procurement.services import registrations

        registrations.register()

        from procurement.views import change_kinds

        change_kinds.register_kinds()  # batch commit / reverse go through approvals (DV-155)
