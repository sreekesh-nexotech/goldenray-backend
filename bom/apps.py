from django.apps import AppConfig


class BomConfig(AppConfig):
    name = "bom"
    label = "bom"
    verbose_name = "BOM"

    def ready(self):
        from bom.services import registrations

        registrations.register()

        from bom.views import change_kinds

        change_kinds.register_kinds()  # what Project Head / Procurement change through approvals
