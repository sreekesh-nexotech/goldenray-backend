from django.apps import AppConfig


class AuditConfig(AppConfig):
    name = "audit"
    label = "audit"
    verbose_name = "Audit log"

    def ready(self):
        from audit import ops  # noqa: F401 - registers the audit_partitions health check and the ops report section
