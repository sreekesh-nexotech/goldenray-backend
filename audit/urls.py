"""Audit log URL lists, mounted by flarize/urls.py under the versioned surfaces.

Owns: staff ``audit/``.
"""

from django.urls import path

from audit.views.audit_log import AuditLogViewSet

staff_urlpatterns = [
    path("audit/", AuditLogViewSet.as_view({"get": "list"}), name="audit-list"),
]
public_urlpatterns: list = []
agent_urlpatterns: list = []
customer_urlpatterns: list = []
