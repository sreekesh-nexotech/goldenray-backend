from django.apps import AppConfig


class CareersConfig(AppConfig):
    name = "careers"
    label = "careers"
    verbose_name = "Careers"

    def ready(self):
        from careers.models import JobApplication, JobPosition
        from careers.services.applications import MEDIA_FOLDER
        from media import folders, usage

        # Resumes and portfolios are candidate PII: hidden from the media library, served only through
        # careers/applications/<uid>/download/<kind>/ (applications.view).
        folders.reserve(MEDIA_FOLDER)
        usage.register(JobApplication, "resume")
        usage.register(JobApplication, "portfolio")
        usage.register(JobPosition, "og_image")
