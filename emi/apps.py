from django.apps import AppConfig


class EmiConfig(AppConfig):
    name = "emi"
    label = "emi"
    verbose_name = "EMI"

    def ready(self):
        from emi import checks  # noqa: F401 - registers the price-source system checks
