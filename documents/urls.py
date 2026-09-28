"""Documents URL lists, mounted by flarize/urls.py under the versioned surfaces.

Owns: staff ``documents/`` (``jobs/<uid>/``, ``jobs/<uid>/download-url/``, ``download/<token>/``). Rendering is
requested by the owning contexts' own endpoints (quotations, agreements, reports) through ``documents.services``.
"""

from django.urls import path

from documents.views.jobs import DocumentDownloadView, RenderJobDetailView, RenderJobDownloadUrlView

staff_urlpatterns = [
    path("documents/jobs/<uuid:uid>/", RenderJobDetailView.as_view(), name="documents-job-detail"),
    path("documents/jobs/<uuid:uid>/download-url/", RenderJobDownloadUrlView.as_view(), name="documents-job-download-url"),
    path("documents/download/<str:token>/", DocumentDownloadView.as_view(), name="documents-download"),
]
public_urlpatterns: list = []
agent_urlpatterns: list = []
customer_urlpatterns: list = []
