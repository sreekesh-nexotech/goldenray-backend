from django.apps import AppConfig


class QuotationsConfig(AppConfig):
    name = "quotations"
    label = "quotations"
    verbose_name = "Quotations"

    def ready(self):
        # Importing registers the owned scope filter and the customer-timeline provider; register() the rest.
        from quotations.services import registrations, scoping  # noqa: F401

        registrations.register()
