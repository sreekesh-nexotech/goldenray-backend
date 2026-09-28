from django.apps import AppConfig


class DocumentsConfig(AppConfig):
    name = "documents"
    label = "documents"
    verbose_name = "Documents"

    def ready(self):
        from documents import ops  # noqa: F401 - registers the render_queue health check and the ops report section
        from documents.models import RenderJob
        from documents.services.jobs import DOCUMENTS_FOLDER
        from media import folders, usage

        # Rendered PDFs are served only through documents/download/<token>/ (never the media library)…
        folders.reserve(DOCUMENTS_FOLDER)
        # …and their asset can never be deleted while the job references it.
        usage.register(RenderJob, "asset")
