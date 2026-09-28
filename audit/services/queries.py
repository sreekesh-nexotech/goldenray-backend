"""Read paths of the audit log."""

from audit.models import AuditLog


def audit_queryset():
    """Every row with its actor joined (the viewer renders the actor's uid, e-mail and name)."""
    return AuditLog.objects.select_related("actor").order_by("-at", "-id")


def history(object_type: str, object_uid):
    """The audit trail of one object, newest first (served by the (object_type, object_uid, at) index)."""
    return audit_queryset().filter(object_type=object_type, object_uid=object_uid)
