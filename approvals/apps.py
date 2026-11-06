from django.apps import AppConfig


class ApprovalsConfig(AppConfig):
    name = "approvals"
    label = "approvals"
    verbose_name = "Approvals"

    def ready(self):
        from approvals.services import scoping  # noqa: F401 - registers the change_requests "owned" scope
