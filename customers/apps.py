from django.apps import AppConfig


class CustomersConfig(AppConfig):
    name = "customers"
    label = "customers"
    verbose_name = "Customers"

    def ready(self):
        # Importing registers the owned scope filter, the timeline's own provider and the dashboard counters.
        from customers.services import customers, dashboard, merge, timeline  # noqa: F401

        merge.register_builtin_dependants()
