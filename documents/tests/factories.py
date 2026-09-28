import uuid

from documents.services.jobs import request_render

REPORT_PAYLOAD = {
    "title": "Price release 12",
    "reference": "PR-12",
    "generated_at": "2026-09-28 10:00",
    "summary": [{"label": "Components", "value": "148"}],
    "sections": [{"title": "Prices", "items": [{"status": "ok", "message": "Every active component has a list price."}]}],
}


def render(user=None, *, kind="PUBLISH_REPORT", object_type="pricing.pricerelease", object_uid=None, template="default", language="en", payload=None, **kwargs):
    return request_render(kind, object_type, object_uid or uuid.uuid4(), template, language, payload if payload is not None else dict(REPORT_PAYLOAD), user, **kwargs)
