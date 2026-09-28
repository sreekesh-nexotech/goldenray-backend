from django.apps import AppConfig


class MediaConfig(AppConfig):
    name = "media"
    label = "media"
    verbose_name = "Media"

    def ready(self):
        from media import ops  # noqa: F401 - registers the ops report section
