from django.apps import AppConfig


class HrConfig(AppConfig):
    name = "hr"
    label = "hr"
    verbose_name = "HR"

    def ready(self):
        from hr.models import Employee
        from hr.services import dashboard, scopes  # noqa: F401 - registers dashboard counters and record-scope filters
        from hr.services.employee_links import PHOTO_FOLDER
        from media import folders, usage

        # Employee photos are personal data: hidden from the media library, served only through signed URLs issued
        # by hr/employees/ (employees.view).
        folders.reserve(PHOTO_FOLDER)
        usage.register(Employee, "photo")
