"""``/healthz`` — plain Django view (no auth, no throttle, never cached) polled by the VM cron and the deploy script."""

from django.http import JsonResponse
from django.views.decorators.cache import never_cache

from core.health import STATUS_FAIL, run_checks
from flarize.exceptions import error_payload

SAFE_METHODS = ("GET", "HEAD")


@never_cache
def healthz(request):
    if request.method not in SAFE_METHODS:
        response = JsonResponse(error_payload("method_not_allowed", "Method not allowed."), status=405)
        response["Allow"] = ", ".join(SAFE_METHODS)
        return response
    status, checks = run_checks()
    return JsonResponse({"status": status, "checks": checks}, status=503 if status == STATUS_FAIL else 200)
