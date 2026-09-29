from django.apps import AppConfig


class InventoryConfig(AppConfig):
    name = "inventory"
    label = "inventory"
    verbose_name = "Inventory"

    def ready(self):
        from inventory import checks  # noqa: F401 - registers the settings check
        from inventory.services import dashboard, usage

        dashboard.register()  # behind INVENTORY_STOCK
        usage.register()  # stock blocks deleting a component (catalog usage registry)
