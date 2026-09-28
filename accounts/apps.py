from django.apps import AppConfig


class AccountsConfig(AppConfig):
    name = "accounts"
    label = "accounts"
    verbose_name = "Accounts"

    def ready(self):
        from accounts import schema  # noqa: F401 - OpenAPI auth extension
        from accounts.services import dashboard  # noqa: F401 - registers the users dashboard counters
