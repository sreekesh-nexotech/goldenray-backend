"""JSON-LD builders (§6.7).

The scope is explicit on two points: FAQPage schema must be *generated from the
structured FAQ record* rather than typed by hand, and raw JSON must not be put
in front of normal content users. So nothing here reads a JSON field an author
filled in — each builder reads the record's own columns and emits the document.
``schema_extra`` only supplies the handful of values that genuinely cannot be
inferred (a salary range, an employment agency), and it is merged last so it can
fill gaps but never rewrite a fact the record already states.

Every builder returns a plain dict or ``None``. Serialising and embedding is the
frontend's job.
"""

from __future__ import annotations

CONTEXT = "https://schema.org"


def _clean(value):
    """Drop empty branches so the emitted JSON-LD has no null-valued keys."""
    if isinstance(value, dict):
        out = {k: _clean(v) for k, v in value.items()}
        return {k: v for k, v in out.items() if v not in (None, "", [], {})}
    if isinstance(value, list):
        items = [_clean(v) for v in value]
        return [v for v in items if v not in (None, "", [], {})]
    return value


def _merge_extra(doc: dict, extra: dict | None) -> dict:
    """Merge author-supplied extras without letting them overwrite record facts."""
    if not extra:
        return doc
    for key, value in extra.items():
        if key.startswith("@"):
            continue  # @context/@type are ours to set
        doc.setdefault(key, value)
    return doc


def _abs_url(site_url: str, path: str) -> str:
    if not path:
        return site_url.rstrip("/")
    if path.startswith(("http://", "https://")):
        return path
    return f"{site_url.rstrip('/')}/{path.lstrip('/')}"


def faq_page(faqs, *, site_url: str = "", page_url: str = "") -> dict | None:
    """FAQPage from an ordered iterable of published FAQ records (§6.5).

    Answers are stored as plain text or light HTML; schema.org accepts HTML in
    ``acceptedAnswer.text``, so the stored answer goes through unchanged rather
    than being flattened and losing its list markup.
    """
    entities = [
        {
            "@type": "Question",
            "name": faq.question,
            "acceptedAnswer": {"@type": "Answer", "text": faq.answer},
        }
        for faq in faqs
        if (faq.question or "").strip() and (faq.answer or "").strip()
    ]
    if not entities:
        return None

    doc = {"@context": CONTEXT, "@type": "FAQPage", "mainEntity": entities}
    if page_url:
        doc["url"] = _abs_url(site_url, page_url)
    return _clean(doc)


def job_posting(position, *, site_url: str = "", organisation: str = "", logo_url: str = "") -> dict | None:
    """JobPosting from a published position (§6.12).

    ``validThrough`` comes from the application deadline when one is set; a
    posting with no deadline simply omits it rather than inventing a date, since
    a wrong expiry silently delists the job from Google Jobs.
    """
    if not (position.title or "").strip():
        return None

    doc = {
        "@context": CONTEXT,
        "@type": "JobPosting",
        "title": position.title,
        "description": position.description,
        "employmentType": position.employment_type,
        "datePosted": position.published_at.date().isoformat() if position.published_at else None,
        "validThrough": position.application_deadline.isoformat() if position.application_deadline else None,
        "hiringOrganization": {
            "@type": "Organization",
            "name": organisation,
            "logo": logo_url,
            "sameAs": site_url,
        },
        "jobLocation": {
            "@type": "Place",
            "address": {"@type": "PostalAddress", "addressLocality": position.location},
        },
        "url": _abs_url(site_url, position.seo_path()),
    }
    if position.department_id:
        doc["occupationalCategory"] = position.department.name
    if position.experience_required:
        doc["experienceRequirements"] = position.experience_required

    return _clean(_merge_extra(doc, position.schema_extra))


def web_page(record, *, site_url: str = "", organisation: str = "") -> dict | None:
    """Generic WebPage for a maintained site page (§6.2)."""
    name = (record.seo_title or record.seo_fallback_title() or "").strip()
    if not name:
        return None

    doc = {
        "@context": CONTEXT,
        "@type": "WebPage",
        "name": name,
        "description": record.meta_description,
        "url": _abs_url(site_url, record.seo_path()),
        "isPartOf": {"@type": "WebSite", "name": organisation, "url": site_url},
    }
    return _clean(_merge_extra(doc, record.schema_extra))


def organisation(settings_row, *, site_url: str = "") -> dict | None:
    """Organization document built from Settings (§6.18)."""
    if not (settings_row.company_name or "").strip():
        return None

    doc = {
        "@context": CONTEXT,
        "@type": "Organization",
        "name": settings_row.company_name,
        "url": site_url,
        "email": settings_row.company_email,
        "telephone": settings_row.company_phone,
        "address": {
            "@type": "PostalAddress",
            "streetAddress": settings_row.address_line,
            "addressLocality": settings_row.address_locality,
            "addressRegion": settings_row.address_region,
            "postalCode": settings_row.postal_code,
            "addressCountry": settings_row.country_code,
        },
    }
    return _clean(doc)


#: ``SchemaType`` value → the builder that produces it. Modules look themselves
#: up here so adding a shape is one entry rather than a chain of ifs.
BUILDERS = {
    "FAQPage": faq_page,
    "JobPosting": job_posting,
    "WebPage": web_page,
    "Organization": organisation,
}
