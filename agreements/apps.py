from django.apps import AppConfig


class AgreementsConfig(AppConfig):
    name = "agreements"
    label = "agreements"
    verbose_name = "Agreements"

    def ready(self):
        # Importing registers the owned scope filter and the customer-timeline provider; register() the rest.
        from agreements.services import registrations, scoping  # noqa: F401

        registrations.register()
