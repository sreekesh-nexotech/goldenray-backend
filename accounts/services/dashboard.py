"""Dashboard counters for the ``users`` module (registered at startup by ``AccountsConfig.ready``)."""

from django.db.models import Count, Q

from accounts.models import User
from core import dashboard


@dashboard.register("users")
def user_counts(user) -> dict[str, int]:
    counts = User.objects.aggregate(active=Count("id", filter=Q(is_active=True)), inactive=Count("id", filter=Q(is_active=False)))
    return {"active": counts["active"], "inactive": counts["inactive"]}
