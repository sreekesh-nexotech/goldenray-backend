"""Dashboard counters for the ``blogs`` module (registered at startup by ``BlogConfig.ready``)."""

from django.db.models import Count, Q

from blog.models import Entry
from core import dashboard

Status = Entry.Status


@dashboard.register("blogs")
def entry_counts(user) -> dict[str, int]:
    counts = Entry.objects.aggregate(
        draft=Count("id", filter=Q(status=Status.DRAFT)),
        review=Count("id", filter=Q(status=Status.REVIEW)),
        published=Count("id", filter=Q(status=Status.PUBLISHED)),
        archived=Count("id", filter=Q(status=Status.ARCHIVED)),
        scheduled=Count("id", filter=Q(scheduled_for__isnull=False)),
    )
    return {f"entries_{name}": value for name, value in counts.items()}
