from django.apps import AppConfig


class PricingConfig(AppConfig):
    name = "pricing"
    label = "pricing"
    verbose_name = "Pricing"

    def ready(self):
        from pricing.services import registrations

        registrations.register()

        from pricing.views import change_kinds

        change_kinds.register_kinds()  # what Project Head / Procurement change through approvals
