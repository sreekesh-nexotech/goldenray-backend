from django.apps import AppConfig


class PricingConfig(AppConfig):
    name = "pricing"
    label = "pricing"
    verbose_name = "Pricing"

    def ready(self):
        from pricing.services import registrations

        registrations.register()
