"""The single DRF exception handler and the JSON error views for non-DRF 404/500.

Every error response has the envelope ``{code, message, errors, error_codes}``:

* ``code`` — stable machine string (``validation_error``, ``not_authenticated``, ``stale_version``, …);
* ``message`` — human-readable summary;
* ``errors`` — ``{field: [messages]}`` for validation failures (``{}`` otherwise);
* ``error_codes`` — unique machine codes harvested from DRF ``ErrorDetail.code`` (or the domain code).

Django's ``SuspiciousOperation`` family, raised while DRF parses a body (``TooManyFieldsSent``,
``TooManyFilesSent``, ``RequestDataTooBig``), is a client error: ``413 request_too_large`` for an oversized body,
``400 bad_request`` for the rest. It is logged once at WARNING (``flarize.security``) and never recorded as a
``SystemException``, so anonymous callers cannot flood the error sink.

Unexpected exceptions become ``500 server_error``: logged with the request id and written to
``core.SystemException`` by a writer that never raises. No exception text or stack reaches the client.
"""

from __future__ import annotations

import logging
import math
from collections.abc import Mapping, Sequence

from django.core.exceptions import PermissionDenied as DjangoPermissionDenied
from django.core.exceptions import RequestDataTooBig, SuspiciousOperation
from django.core.exceptions import ValidationError as DjangoValidationError
from django.http import Http404, JsonResponse
from rest_framework import exceptions as drf_exceptions
from rest_framework import status
from rest_framework.response import Response
from rest_framework.views import set_rollback

from core.errors import DomainError

logger = logging.getLogger("flarize.errors")
security_logger = logging.getLogger("flarize.security")

NON_FIELD_ERRORS_KEY = "non_field_errors"

# DRF exception class → (status, envelope code, default message)
_API_EXCEPTION_CODES: tuple[tuple[type[drf_exceptions.APIException], str, str], ...] = (
    (drf_exceptions.NotAuthenticated, "not_authenticated", "Authentication credentials were not provided or are invalid."),
    (drf_exceptions.AuthenticationFailed, "not_authenticated", "Authentication credentials were not provided or are invalid."),
    (drf_exceptions.PermissionDenied, "permission_denied", "You do not have permission to perform this action."),
    (drf_exceptions.NotFound, "not_found", "Not found."),
    (drf_exceptions.MethodNotAllowed, "method_not_allowed", "Method not allowed."),
    (drf_exceptions.NotAcceptable, "not_acceptable", "Could not satisfy the request Accept header."),
    (drf_exceptions.UnsupportedMediaType, "unsupported_media_type", "Unsupported media type."),
    (drf_exceptions.ParseError, "parse_error", "Malformed request body."),
    (drf_exceptions.Throttled, "throttled", "Request was throttled."),
)


def error_payload(code: str, message: str, errors: Mapping | None = None, error_codes: Sequence[str] | None = None) -> dict:
    return {"code": code, "message": message, "errors": dict(errors or {}), "error_codes": list(error_codes or [code])}


def _flatten_messages(detail) -> list[str]:
    if isinstance(detail, Mapping):
        return [message for value in detail.values() for message in _flatten_messages(value)]
    if isinstance(detail, (list, tuple)):
        return [message for value in detail for message in _flatten_messages(value)]
    return [str(detail)]


def _normalise_errors(detail) -> dict:
    """Shape DRF validation detail as ``{field: [messages]}``; nested serializers keep their nested structure."""

    def convert(value):
        if isinstance(value, Mapping):
            return {str(key): convert(item) for key, item in value.items()}
        if isinstance(value, (list, tuple)):
            return [convert(item) for item in value]
        return str(value)

    if isinstance(detail, Mapping):
        errors = {}
        for key, value in detail.items():
            converted = convert(value)
            errors[str(key)] = converted if isinstance(converted, (list, dict)) else [converted]
        return errors
    if isinstance(detail, (list, tuple)):
        return {NON_FIELD_ERRORS_KEY: [convert(item) for item in detail]}
    return {NON_FIELD_ERRORS_KEY: [str(detail)]}


def harvest_codes(detail, default: str | None = "invalid") -> list[str]:
    """Unique ``ErrorDetail.code`` values in traversal order; plain strings count as ``default`` (skipped if None)."""
    codes: list[str] = []

    def walk(value):
        if isinstance(value, Mapping):
            for item in value.values():
                walk(item)
        elif isinstance(value, (list, tuple)):
            for item in value:
                walk(item)
        else:
            code = getattr(value, "code", None) or default
            if code and code not in codes:
                codes.append(code)

    walk(detail)
    return codes


def _validation_response(detail) -> Response:
    errors = _normalise_errors(detail)
    messages = _flatten_messages(detail)
    message = messages[0] if len(messages) == 1 and NON_FIELD_ERRORS_KEY in errors else "Invalid input."
    return Response(error_payload("validation_error", message, errors, harvest_codes(detail)), status=status.HTTP_400_BAD_REQUEST)


def _domain_response(exc: DomainError) -> Response:
    errors = _normalise_errors(exc.errors) if exc.errors else {}
    codes = [exc.code]
    if exc.errors:
        codes += [code for code in harvest_codes(exc.errors, default=None) if code not in codes]
    headers = {}
    retry_after = getattr(exc, "retry_after", None)  # e.g. accounts.errors.LoginLocked (429)
    if retry_after is not None:
        headers["Retry-After"] = str(max(1, math.ceil(retry_after)))
    return Response(error_payload(exc.code, exc.message, errors, codes), status=exc.status, headers=headers)


def _api_exception_response(exc: drf_exceptions.APIException) -> Response:
    headers = {}
    if getattr(exc, "auth_header", None):
        headers["WWW-Authenticate"] = exc.auth_header
    if isinstance(exc, drf_exceptions.Throttled) and exc.wait is not None:
        headers["Retry-After"] = str(max(1, math.ceil(exc.wait)))
    for exc_class, code, default_message in _API_EXCEPTION_CODES:
        if isinstance(exc, exc_class):
            message = str(exc.detail) if isinstance(exc.detail, str) else default_message
            codes = [code]
            if isinstance(exc.detail, Mapping) and isinstance(exc.detail.get("detail"), str):
                # SimpleJWT exceptions carry {"detail", "code"}: surface the reason (e.g. `session_revoked`).
                message = str(exc.detail["detail"])
                detail_code = exc.detail.get("code")
                if isinstance(detail_code, str) and detail_code and detail_code != code:
                    codes.append(detail_code)
            if isinstance(exc, drf_exceptions.Throttled):
                message = default_message
            return Response(error_payload(code, message, error_codes=codes), status=exc.status_code, headers=headers)
    if isinstance(exc.detail, str):
        code, message = getattr(exc.detail, "code", None) or exc.default_code, str(exc.detail)
    else:
        code, message = exc.default_code, str(exc.default_detail)
    return Response(error_payload(str(code), message), status=exc.status_code, headers=headers)


def _suspicious_response(exc: SuspiciousOperation, context: Mapping | None) -> Response:
    """Client error, not a server fault: no SystemException, no ERROR line, and no exception text in the body."""
    request = (context or {}).get("request")
    security_logger.warning("Rejected request: %s", exc.__class__.__name__, extra={"path": getattr(request, "path", ""), "method": getattr(request, "method", "")})
    if isinstance(exc, RequestDataTooBig):
        return Response(error_payload("request_too_large", "The request body is too large."), status=status.HTTP_413_REQUEST_ENTITY_TOO_LARGE)
    return Response(error_payload("bad_request", "Bad request."), status=status.HTTP_400_BAD_REQUEST)


def _record_unexpected(exc: Exception, context: Mapping | None) -> None:
    request = (context or {}).get("request")
    logger.error("Unhandled exception: %s", exc.__class__.__name__, exc_info=(type(exc), exc, exc.__traceback__))
    from core.services.system_exceptions import record_exception

    record_exception(exc, request=request, source="api")


def exception_handler(exc: Exception, context: Mapping) -> Response:
    """DRF ``EXCEPTION_HANDLER``. Always returns a response; never re-raises."""
    if isinstance(exc, Http404):
        exc = drf_exceptions.NotFound()
    elif isinstance(exc, DjangoPermissionDenied):
        exc = drf_exceptions.PermissionDenied()

    if isinstance(exc, DomainError):
        set_rollback()
        return _domain_response(exc)
    if isinstance(exc, drf_exceptions.ValidationError):
        set_rollback()
        return _validation_response(exc.detail)
    if isinstance(exc, DjangoValidationError):
        set_rollback()
        detail = exc.message_dict if hasattr(exc, "error_dict") else exc.messages
        return _validation_response(drf_exceptions.ValidationError(detail).detail)
    if isinstance(exc, drf_exceptions.APIException):
        set_rollback()
        return _api_exception_response(exc)
    if isinstance(exc, SuspiciousOperation):
        set_rollback()
        return _suspicious_response(exc, context)

    set_rollback()
    _record_unexpected(exc, context)
    response = Response(error_payload("server_error", "Internal server error."), status=status.HTTP_500_INTERNAL_SERVER_ERROR)
    # Logged above (traceback + request id): Django's handler must not log it again after RequestIdMiddleware
    # has reset the request context (a second ERROR line without the request id).
    response._has_been_logged = True
    return response


# ------------------------------------------------------------------------------------------------------------------
# Django-level handlers (non-DRF paths: unmatched URLs, plain views)
# ------------------------------------------------------------------------------------------------------------------
def not_found_view(request, exception=None):
    return JsonResponse(error_payload("not_found", "Not found."), status=404)


def server_error_view(request):
    return JsonResponse(error_payload("server_error", "Internal server error."), status=500)


def bad_request_view(request, exception=None):
    return JsonResponse(error_payload("bad_request", "Bad request."), status=400)


def permission_denied_view(request, exception=None):
    return JsonResponse(error_payload("permission_denied", "You do not have permission to perform this action."), status=403)
