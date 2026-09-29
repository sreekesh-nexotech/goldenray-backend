from django.apps import AppConfig


class AttendanceConfig(AppConfig):
    name = "attendance"
    label = "attendance"
    verbose_name = "Attendance"

    def ready(self):
        from attendance.services import dashboard, scopes, sink  # noqa: F401 - scopes registers the attendance filters

        # the punch store behind devices' ingestion (agent uploads, ADMS pushes), DV-78
        sink.install()
        # hr's office summary / dependency counters and the dashboard counters
        dashboard.install()
