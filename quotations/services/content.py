"""Quotation content (PLAN §1.2 ContentRelease, §3.4 ``quotation-content/…``): bilingual content versions with the
fit guard, and the four masters printed in every quotation — inclusions, tier display names, testimonials, campaigns.

Content versions follow ``quotationContent.js``: one open DRAFT; saving and publishing both run the fit guard
(``engines.content_fit``) and refuse content that does not fit its page (422 ``content_invalid`` with every error);
``fit-check/`` stores the report without refusing. Publishing a DRAFT makes it the single PUBLISHED version (the
previous one becomes SUPERSEDED) and freezes the four masters beside it (``release_payload``, in the Flarize shapes
the payload assembler reads) — the ContentRelease new quotations pin. Issued quotations keep the one they printed.
"""

from __future__ import annotations

import datetime as dt
import re

from django.db import IntegrityError, models, transaction
from django.db.models import Max
from django.utils import timezone

from audit.services import changes, record, snapshot
from core.errors import Conflict, DomainError, NotFound
from core.outbox import emit
from core.services import check_version, stamp_create
from engines.content_fit import FALLBACK_DAILY_GEN_PER_KW, fit_summary
from engines.frozen import sha256_hex
from flarize.cache_utils import bump
from quotations.models import Campaign, ContentStatus, ContentVersion, Inclusion, InclusionKind, SystemType, Testimonial, TierDisplayName
from quotations.services.common import CONTENT_NAMESPACE, PUBLIC_TESTIMONIALS_NAMESPACE, TIER_ORDER, js_number, plain

TIER_KEYS = {"BASE": "Base", "VALUE": "Value", "PREMIUM": "Premium"}


class ContentInvalid(DomainError):
    status = 422
    default_code = "content_invalid"
    default_message = "The quotation content does not fit the document pages."


# ── content versions ───────────────────────────────────────────────────────────────────────────────────────────────


def versions_queryset():
    return ContentVersion.objects.select_related("published_by", "created_by")


def published_version() -> ContentVersion | None:
    return ContentVersion.objects.filter(status=ContentStatus.PUBLISHED).first()


def daily_gen_per_kw():
    """The live yield (kWh/kW/day) the appliance-profile fit rule uses: the current PriceRelease's energy configuration."""
    from pricing.services.releases import current_release

    release = current_release()
    energy = ((release.payload if release else {}) or {}).get("cost_config", {}).get("energy.config") or {}
    region = (energy.get("regions") or {}).get(energy.get("defaultRegion") or "") or next(iter((energy.get("regions") or {}).values()), {})
    value = ((region or {}).get("yield") or {}).get("dailyGenPerKw")
    return value if value is not None else js_number(FALLBACK_DAILY_GEN_PER_KW)


def _fit(content) -> dict:
    return plain(fit_summary(content, daily_gen_per_kw()))


def _require_fit(report: dict) -> None:
    if not report["ok"]:
        errors: dict[str, list[str]] = {}
        for error in report["errors"]:
            errors.setdefault(error.get("path") or "content", []).append(f"{error.get('code')}: {error.get('message')}")
        raise ContentInvalid(errors=errors)


def _validate_payload(payload) -> None:
    if not isinstance(payload, dict) or not payload:
        raise DomainError("validation_error", "language_payload must be a non-empty object.", errors={"language_payload": ["Must be a non-empty object."]})


@transaction.atomic
def create_draft(*, user, data: dict) -> ContentVersion:
    """A new DRAFT: the given ``language_payload`` or a copy of the published content (409 ``draft_exists``)."""
    if ContentVersion.objects.filter(status=ContentStatus.DRAFT).exists():
        raise Conflict("draft_exists", "A content draft is already open; edit or publish it.")
    payload = data.get("language_payload")
    if payload is None:
        published = published_version()
        if published is None:
            raise DomainError("validation_error", "No published content to copy; send language_payload.", errors={"language_payload": ["Required."]})
        payload = published.language_payload
    _validate_payload(payload)
    report = _fit(payload)
    _require_fit(report)
    number = (ContentVersion.all_objects.aggregate(top=Max("number"))["top"] or 0) + 1
    version = ContentVersion(number=number, language_payload=payload, fit_report=report, note=data.get("note", ""))
    stamp_create(version, user)
    try:
        with transaction.atomic():
            version.save()
    except IntegrityError:
        raise Conflict("draft_exists", "A content draft is already open; edit or publish it.") from None
    record("quotation_content.draft_created", obj=version, actor=user, after={"number": number})
    bump(CONTENT_NAMESPACE)
    return version


def _locked(version: ContentVersion, expected_version) -> ContentVersion:
    locked = ContentVersion.objects.select_for_update().filter(pk=version.pk).first()
    if locked is None:
        raise NotFound("not_found", "Content version not found.")
    check_version(locked, expected_version)
    return locked


def _require_draft(version: ContentVersion) -> None:
    if version.status != ContentStatus.DRAFT:
        raise Conflict("version_not_draft", "Only the DRAFT content version can be changed.", errors={"status": [version.status]})


@transaction.atomic
def update_draft(version: ContentVersion, *, user, data: dict, expected_version=None) -> ContentVersion:
    version = _locked(version, expected_version)
    _require_draft(version)
    values = {}
    if "language_payload" in data:
        _validate_payload(data["language_payload"])
        report = _fit(data["language_payload"])
        _require_fit(report)
        values.update(language_payload=data["language_payload"], fit_report=report)
    if "note" in data:
        values["note"] = data["note"]
    if values:
        version.versioned_update(user, **values)
        record("quotation_content.draft_updated", obj=version, actor=user, after={"fields": sorted(values)})
        bump(CONTENT_NAMESPACE)
    return version


@transaction.atomic
def fit_check(version: ContentVersion, *, user, language_payload=None) -> dict:
    """Run the fit guard; on the DRAFT the report is stored. ``language_payload`` checks a candidate without saving."""
    if language_payload is not None:
        _validate_payload(language_payload)
        return _fit(language_payload)
    version = _locked(version, None)
    report = _fit(version.language_payload)
    if version.status == ContentStatus.DRAFT and report != version.fit_report:
        version.versioned_update(user, fit_report=report)
    return report


def release_payload(*, on: dt.date | None = None) -> dict:
    """The four masters in the shapes the payload assembler reads, as they are now (frozen into a ContentRelease)."""
    return {
        "inclusionMatrix": inclusion_matrix(),
        "tierDisplayNames": tier_display_names(),
        "testimonials": testimonial_entries(),
        "campaigns": [campaign_value(campaign) for campaign in Campaign.objects.filter(is_active=True).order_by("-starts_on", "-id")],
    }


@transaction.atomic
def publish(version: ContentVersion, *, user, note: str = "", expected_version=None) -> ContentVersion:
    version = _locked(version, expected_version)
    _require_draft(version)
    report = _fit(version.language_payload)
    _require_fit(report)
    now = timezone.now()
    previous = ContentVersion.objects.select_for_update().filter(status=ContentStatus.PUBLISHED).first()
    if previous is not None:
        previous.versioned_update(user, status=ContentStatus.SUPERSEDED, superseded_at=now)
    payload = release_payload(on=timezone.localdate())
    version.versioned_update(
        user,
        status=ContentStatus.PUBLISHED,
        fit_report=report,
        release_payload=payload,
        release_sha256=sha256_hex(payload),
        published_at=now,
        published_by=user if getattr(user, "pk", None) else None,
        note=note or version.note,
    )
    record("quotation_content.published", obj=version, actor=user, after={"number": version.number, "previous": previous.number if previous else None})
    emit(
        "quotation_content.published",
        {"content_version_uid": str(version.uid), "number": version.number, "previous_number": previous.number if previous else None, "release_sha256": version.release_sha256},
        aggregate_type="quotations.contentversion",
        aggregate_uid=version.uid,
        dedup_key=f"quotation_content.published:{version.number}",
    )
    bump(CONTENT_NAMESPACE)
    return version


# ── the masters in the Flarize shapes ─────────────────────────────────────────────────────────────────────────────


def inclusion_matrix() -> dict | None:
    """``quotation-inclusions.json``: ``{Base|Value|Premium: {_tierCode, <key>: bool|null}, _serviceMatrix: [...]}``."""
    rows = list(Inclusion.objects.order_by("sort_order", "id"))
    if not rows:
        return None
    matrix: dict = {name: {"_tierCode": name.lower()} for name in TIER_KEYS.values()}
    service = []
    for row in rows:
        applies = row.applies_to or {}
        if row.kind == InclusionKind.SERVICE:
            entry = {"label": row.label_en}
            if row.label_ml:
                entry["labelMl"] = row.label_ml
            for tier, name in TIER_KEYS.items():
                entry[name.lower()] = applies.get(tier)
            service.append(entry)
            continue
        for tier, name in TIER_KEYS.items():
            value = applies.get(tier, row.default_on)
            matrix[name][row.key] = value if isinstance(value, bool) or value is None else bool(value)
    if service:
        matrix["_serviceMatrix"] = service
    matrix["_labels"] = {row.key: {"en": row.label_en, "ml": row.label_ml} for row in rows if row.kind == InclusionKind.COMPONENT}
    return matrix


def tier_display_names() -> dict:
    """``{ONGRID|HYBRID: {en: {base, value, premium, recommendedTier, recommendedBadge}, ml: {…}}}``."""
    out: dict = {}
    for row in TierDisplayName.objects.order_by("system_type", "tier"):
        entry = out.setdefault(row.system_type, {"en": {}, "ml": {}})
        tier = row.tier.lower()
        entry["en"][tier] = row.name_en
        entry["ml"][tier] = row.name_ml or row.name_en
        if row.is_recommended:
            for lang, badge in (("en", row.badge_en), ("ml", row.badge_ml or row.badge_en)):
                entry[lang]["recommendedTier"] = tier
                entry[lang]["recommendedBadge"] = badge or None
    return out


def _photo_uri(testimonial: Testimonial) -> str | None:
    if testimonial.photo_id and testimonial.photo and testimonial.photo.cdn_url:
        return testimonial.photo.cdn_url
    return testimonial.photo_url or None


def testimonial_entries() -> list[dict]:
    """Active testimonials (quotation page 6), with both languages; the payload takes the quotation's language."""
    entries = []
    for row in Testimonial.objects.filter(is_active=True).select_related("photo").order_by("sort_order", "id"):
        saving = row.bill_before - row.bill_after if row.bill_before is not None and row.bill_after is not None else None
        entries.append(
            {
                "name": row.customer_name,
                "place": row.location or None,
                "systemKw": js_number(row.capacity_kw.normalize()) if row.capacity_kw is not None else None,
                "systemLabel": row.system_label or None,
                "installedOn": row.installed_on.strftime("%B %Y") if row.installed_on else (row.installed_on_label or None),
                "quote": row.quote_en,
                "quoteMl": row.quote_ml or None,
                "billBefore": js_number(row.bill_before.normalize()) if row.bill_before is not None else None,
                "billAfter": js_number(row.bill_after.normalize()) if row.bill_after is not None else None,
                "monthlySaving": js_number(saving.normalize()) if saving is not None else None,
                "photoUri": _photo_uri(row),
            }
        )
    return entries


def campaign_value(campaign: Campaign) -> dict:
    return {
        "id": str(campaign.uid),
        "title": campaign.title,
        "bodyEn": campaign.body_en,
        "bodyMl": campaign.body_ml,
        "imageUri": campaign.image.cdn_url if campaign.image_id and campaign.image and campaign.image.cdn_url else None,
        "startsOn": campaign.starts_on.isoformat() if campaign.starts_on else None,
        "endsOn": campaign.ends_on.isoformat() if campaign.ends_on else None,
    }


def active_campaign(campaigns: list[dict], on: dt.date) -> dict | None:
    """The campaign printed on page 2: active, its window contains ``on``; the latest start wins."""
    day = on.isoformat()
    live = [c for c in campaigns or [] if (not c.get("startsOn") or c["startsOn"] <= day) and (not c.get("endsOn") or day <= c["endsOn"])]
    return live[0] if live else None


def campaign_result(campaign: dict | None, language: str) -> dict | None:
    """The ``campaignResult`` the assembler accepts (``resolveCampaignResult``)."""
    if not campaign:
        return None
    body = campaign.get("bodyMl") if language == "ml" and campaign.get("bodyMl") else campaign.get("bodyEn")
    return {
        "available": True,
        "source": "QUOTATION_CAMPAIGN",
        "id": campaign["id"],
        "name": campaign["title"],
        "active": True,
        "headline": campaign["title"],
        "subheadline": body or None,
        "primaryBenefit": None,
        "offers": [],
        "heroImage": campaign.get("imageUri"),
        "period": {"displayText": " – ".join(filter(None, [campaign.get("startsOn"), campaign.get("endsOn")])) or None},
    }


def testimonials_content(entries: list[dict] | None, language: str) -> dict | None:
    """``quotation-testimonials.json`` shape, the quotes in the quotation's language (English when not translated)."""
    if entries is None:
        return None
    rows = [{**entry, "quote": (entry.get("quoteMl") or entry["quote"]) if language == "ml" else entry["quote"]} for entry in entries]
    return {"intro": None, "entries": rows, "video": None}


def tier_names_for(names: dict | None, system_type: str) -> dict | None:
    """The engine's ``tierDisplayNames`` (English names; Flarize's names are one set for every system type)."""
    entry = (names or {}).get(system_type) or {}
    english = entry.get("en")
    return dict(english) if english else None


# ── master CRUD ────────────────────────────────────────────────────────────────────────────────────────────────────

INCLUSION_FIELDS = ("key", "kind", "label_en", "label_ml", "default_on", "applies_to", "sort_order")
TIER_NAME_FIELDS = ("system_type", "tier", "name_en", "name_ml", "is_recommended", "badge_en", "badge_ml")
TESTIMONIAL_FIELDS = (
    "customer_name",
    "location",
    "capacity_kw",
    "system_label",
    "installed_on",
    "installed_on_label",
    "quote_en",
    "quote_ml",
    "bill_before",
    "bill_after",
    "photo",
    "photo_url",
    "is_active",
    "sort_order",
    "show_on_website",
)
CAMPAIGN_FIELDS = ("title", "body_en", "body_ml", "image", "starts_on", "ends_on", "is_active")
KEY_RE = re.compile(r"^[A-Za-z][A-Za-z0-9_]{0,63}$")


def _namespaces(model) -> tuple[str, ...]:
    return (CONTENT_NAMESPACE, PUBLIC_TESTIMONIALS_NAMESPACE) if model is Testimonial else (CONTENT_NAMESPACE,)


def _action(model) -> str:
    return f"quotation_content.{model._meta.model_name}"


def _check_rules(model, instance) -> None:
    errors: dict[str, list[str]] = {}
    if model is Inclusion:
        if not KEY_RE.match(instance.key or ""):
            errors["key"] = ["Use a letter, then letters, digits or underscores (max 64)."]
        applies = instance.applies_to if isinstance(instance.applies_to, dict) else None
        if applies is None or any(tier not in TIER_KEYS for tier in applies):
            errors["applies_to"] = ["An object keyed by BASE, VALUE, PREMIUM."]
        elif any(not (isinstance(value, (bool, str)) or value is None) for value in applies.values()):
            errors["applies_to"] = ["Values are true, false, null or a text."]
        elif instance.kind == InclusionKind.COMPONENT and any(isinstance(value, str) for value in applies.values()):
            errors["applies_to"] = ["A component inclusion is true, false or null per tier."]
    if model is Testimonial and instance.bill_before is not None and instance.bill_after is not None and instance.bill_after > instance.bill_before:
        errors["bill_after"] = ["The bill after solar cannot be higher than before."]
    if model is Testimonial and instance.photo_id and (instance.photo.visibility != "PUBLIC" or instance.photo.kind not in ("IMAGE", "PHOTO")):
        errors["photo"] = ["Use a public image."]
    if model is Campaign and instance.image_id and (instance.image.visibility != "PUBLIC" or instance.image.kind not in ("IMAGE", "PHOTO")):
        errors["image"] = ["Use a public image."]
    if model is Campaign and instance.starts_on and instance.ends_on and instance.ends_on < instance.starts_on:
        errors["ends_on"] = ["Must be on or after starts_on."]
    if errors:
        raise DomainError("validation_error", "Invalid values.", errors=errors)


def _save_new(model, instance, conflict_code: str) -> None:
    try:
        with transaction.atomic():
            instance.save()
    except IntegrityError:
        raise Conflict(conflict_code, f"A {model._meta.verbose_name} with these values already exists.") from None


def _demote_recommended(instance: TierDisplayName, user) -> None:
    if instance.is_recommended:
        for other in TierDisplayName.objects.select_for_update().filter(system_type=instance.system_type, is_recommended=True).exclude(pk=instance.pk):
            other.versioned_update(user, is_recommended=False)


def _factory(model, fields: tuple[str, ...], conflict_code: str):
    @transaction.atomic
    def create(*, user, data: dict) -> models.Model:
        instance = model(**{name: data[name] for name in fields if name in data})
        _check_rules(model, instance)
        stamp_create(instance, user)
        if model is TierDisplayName:
            _demote_recommended(instance, user)
        _save_new(model, instance, conflict_code)
        record(f"{_action(model)}_created", obj=instance, actor=user, after=snapshot(instance, fields))
        bump(*_namespaces(model))
        return instance

    @transaction.atomic
    def update(instance, *, user, data: dict, expected_version=None) -> models.Model:
        locked = model.objects.select_for_update().filter(pk=instance.pk).first()
        if locked is None:
            raise NotFound("not_found", f"{model._meta.verbose_name.capitalize()} not found.")
        check_version(locked, expected_version)
        before = snapshot(locked, fields)
        values = {name: data[name] for name in fields if name in data}
        for name, value in values.items():
            setattr(locked, name, value)
        _check_rules(model, locked)
        if model is TierDisplayName:
            _demote_recommended(locked, user)
        try:
            with transaction.atomic():
                locked.versioned_update(user, **values)
        except IntegrityError:
            raise Conflict(conflict_code, f"A {model._meta.verbose_name} with these values already exists.") from None
        old, new = changes(before, snapshot(locked, fields))
        if new:
            record(f"{_action(model)}_updated", obj=locked, actor=user, before=old, after=new)
        bump(*_namespaces(model))
        return locked

    @transaction.atomic
    def destroy(instance, *, user, expected_version=None) -> None:
        locked = model.objects.select_for_update().filter(pk=instance.pk).first()
        if locked is None:
            raise NotFound("not_found", f"{model._meta.verbose_name.capitalize()} not found.")
        check_version(locked, expected_version)
        locked.soft_delete(user)
        record(f"{_action(model)}_deleted", obj=locked, actor=user, before=snapshot(locked, fields))
        bump(*_namespaces(model))

    return create, update, destroy


create_inclusion, update_inclusion, delete_inclusion = _factory(Inclusion, INCLUSION_FIELDS, "inclusion_key_taken")
create_tier_name, update_tier_name, delete_tier_name = _factory(TierDisplayName, TIER_NAME_FIELDS, "tier_name_exists")
create_testimonial, update_testimonial, delete_testimonial = _factory(Testimonial, TESTIMONIAL_FIELDS, "testimonial_conflict")
create_campaign, update_campaign, delete_campaign = _factory(Campaign, CAMPAIGN_FIELDS, "campaign_conflict")


def public_testimonials():
    """``GET /api/public/v1/testimonials/``: active testimonials marked ``show_on_website``, in display order."""
    return Testimonial.objects.filter(is_active=True, show_on_website=True).select_related("photo").order_by("sort_order", "id")


def system_types() -> list[str]:
    return list(SystemType.values)


TIERS = TIER_ORDER
