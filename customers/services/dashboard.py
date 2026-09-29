"""``dashboard/`` counters for the ``customers`` module (record scope applied)."""

from __future__ import annotations

from datetime import timedelta

from django.utils import timezone

from core import dashboard
from customers.services.customers import visible_customers


@dashboard.register("customers")
def customer_counts(user) -> dict[str, int]:
    visible = visible_customers(user)
    return {"total": visible.count(), "new_30d": visible.filter(created_at__gte=timezone.now() - timedelta(days=30)).count()}
