from django.apps import AppConfig


class AccountsConfig(AppConfig):
    name = "accounts"
    label = "accounts"
    verbose_name = "Accounts"

    def ready(self):
        from accounts import schema  # noqa: F401 - OpenAPI auth extension
