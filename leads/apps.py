from django.apps import AppConfig


class LeadsConfig(AppConfig):
    name = "leads"
    label = "leads"
    verbose_name = "Leads"

    def ready(self):
        from customers.services import merge
        from leads.models import CustomerInstallation, Lead, WarrantyRequest
        from leads.services import customer_timeline, dashboard, scoping  # noqa: F401 - registers scope filter, counters, timeline providers
        from media import usage

        # A customer merge re-points these columns to the surviving customer (they never block deleting one).
        merge.register_dependant(Lead, "customer")
        merge.register_dependant(WarrantyRequest, "customer")
        usage.register(CustomerInstallation, "photo")
