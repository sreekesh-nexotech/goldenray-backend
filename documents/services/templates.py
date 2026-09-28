"""Document templates: ``documents/<kind>/<language>.html`` (template ``default``) or
``documents/<kind>/<template>/<language>.html`` (a named variant, e.g. an inspection report's ``customer`` and
``internal`` variants). Kinds are lower-cased in paths (``documents/publish_report/ml.html``).

Owning apps ship their templates in ``<app>/templates/documents/<kind>/…`` (found through ``APP_DIRS``); every
template extends ``documents/base.html``, which fixes the A4 page and declares the Noto fonts for en/ml/hi.
Templates are rendered with Django's autoescaping; the payload is data, never markup.
"""

from __future__ import annotations

import re

from django.template import TemplateDoesNotExist, TemplateSyntaxError
from django.template.loader import get_template

from core.errors import DomainError

TEMPLATE_RE = re.compile(r"^[a-z0-9][a-z0-9_-]{0,63}$")
DEFAULT_TEMPLATE = "default"


def template_name(kind: str, template: str, language: str) -> str:
    base = f"documents/{kind.lower()}"
    if template == DEFAULT_TEMPLATE:
        return f"{base}/{language}.html"
    return f"{base}/{template}/{language}.html"


def ensure_template(kind: str, template: str, language: str) -> str:
    if not TEMPLATE_RE.match(template or ""):
        raise DomainError("validation_error", "Invalid template name.", errors={"template": ["Use lower-case letters, digits, '-' and '_' (max 64)."]})
    name = template_name(kind, template, language)
    try:
        get_template(name)
    except TemplateDoesNotExist:
        raise DomainError("template_not_found", f"No {language} template {template!r} exists for {kind}.", errors={"template": [f"{name} does not exist."]}) from None
    except TemplateSyntaxError as exc:
        raise DomainError("template_invalid", "The document template is broken.", status=500, errors={"template": [str(exc)[:300]]}) from None
    return name


def render_html(job) -> str:
    context = {
        "payload": job.payload,
        "language": job.language,
        "kind": job.kind,
        "job_uid": str(job.uid),
        "object_uid": str(job.object_uid),
        "requested_at": job.created_at,
    }
    return get_template(template_name(job.kind, job.template, job.language)).render(context)
