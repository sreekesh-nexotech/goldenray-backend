from django.apps import AppConfig


class CoreConfig(AppConfig):
    name = "core"
    label = "core"
    verbose_name = "Platform core"

    def ready(self):
        from core import checks  # noqa: F401 - registers system checks
        from core import health  # noqa: F401 - registers built-in health checks
        from core import schema  # noqa: F401 - OpenAPI auth extension
        from core.outbox import autodiscover_handlers

        autodiscover_handlers()
