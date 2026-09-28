"""``/healthz`` — plain Django view (no auth, no throttle, never cached) polled by the VM cron and the deploy script."""

from django.http import JsonResponse
from django.views.decorators.cache import never_cache
from django.views.decorators.http import require_safe

from core.health import STATUS_FAIL, run_checks


@never_cache
@require_safe
def healthz(request):
    status, checks = run_checks()
    return JsonResponse({"status": status, "checks": checks}, status=503 if status == STATUS_FAIL else 200)
