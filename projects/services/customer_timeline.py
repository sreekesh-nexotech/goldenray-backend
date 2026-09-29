"""The projects context's entries in ``customers/<uid>/timeline/`` (registered on import, from ``ProjectsConfig.ready``).

One entry per milestone of each live project of the customer: created, BOM locked, commissioned, closed, cancelled.
Asked only when the viewer holds ``projects.view`` (the timeline checks it); the ``projects`` module has only the
``all`` scope (PLAN §3.2), so there is no narrower record filter to apply.
"""

from __future__ import annotations

from core import scopes
from customers.services import timeline
from projects.models import Project

MILESTONES = (
    ("created_at", "projects.created", "Project {number} created"),
    ("bom_locked_at", "projects.bom_locked", "Project {number}: BOM locked"),
    ("commissioned_at", "projects.commissioned", "Project {number} commissioned"),
    ("closed_at", "projects.closed", "Project {number} closed"),
    ("cancelled_at", "projects.cancelled", "Project {number} cancelled"),
)


@timeline.register("projects.projects", module="projects")
def project_entries(customer, *, user, before, limit):
    projects = scopes.apply(Project.objects.filter(customer=customer), user, "projects").order_by("-created_at", "-id")
    entries = []
    for project in projects.only("uid", "number", "status", "created_at", "bom_locked_at", "commissioned_at", "closed_at", "cancelled_at"):  # a customer has a handful of projects
        for column, kind, title in MILESTONES:
            at = getattr(project, column)
            if at is not None and (before is None or at < before):
                entries.append(timeline.Entry(at, kind, title.format(number=project.number), "projects.project", project.uid, {"status": project.status}))
    entries.sort(key=lambda entry: entry.at, reverse=True)
    yield from entries[:limit]
