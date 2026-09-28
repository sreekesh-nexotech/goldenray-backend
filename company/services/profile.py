"""The company profile singleton (``company/profile/`` staff, ``company/`` public).

* :func:`current_profile` is what every read serves: the stored row, or an unsaved one carrying the defaults
  (``uid`` null, ``version`` 1). Reads never write;
* :func:`ensure_profile` creates the one row (race-safe through the singleton unique index), audited as
  ``company.profile_created``; :func:`update_profile` calls it on the first edit that changes something;
* :func:`update_profile` edits it with optimistic locking, validates brand assets (kind, and *public* for anything
  the website shows), writes one audit row, bumps the ``company`` cache namespace and emits
  ``company.profile_updated``;
* :func:`quotation_offer` is the typed view of the quotation display settings (legacy ``bom.QuotationSettings``)
  — what the quotation document prints and what ``/legacy/bom/api/quotation-settings/`` serves.

``blog_revalidate_secret`` is write-only: responses carry ``blog_revalidate_secret_set`` only.
"""

from __future__ import annotations

import datetime as dt
from dataclasses import dataclass

from django.db import IntegrityError, transaction
from django.utils import timezone

from audit.services import changes, record, snapshot
from company.models import CompanyProfile
from core.errors import DomainError
from core.outbox import emit
from core.services import check_version, stamp_create
from flarize.cache_utils import bump
from media.models import MediaAsset

CACHE_NAMESPACE = "company"
SECRET_FIELD = "blog_revalidate_secret"
Kind = MediaAsset.Kind


@dataclass(frozen=True)
class AssetRule:
    kinds: frozenset[str]
    public: bool  # must be a PUBLIC asset (shown on the website)


ASSET_RULES: dict[str, AssetRule] = {
    "logo": AssetRule(frozenset({Kind.IMAGE}), public=True),
    "default_og_image": AssetRule(frozenset({Kind.IMAGE}), public=True),
    "quotation_offer_image": AssetRule(frozenset({Kind.IMAGE}), public=True),
    "letterhead": AssetRule(frozenset({Kind.IMAGE}), public=False),
    "seal": AssetRule(frozenset({Kind.IMAGE, Kind.SIGNATURE}), public=False),
    "signature": AssetRule(frozenset({Kind.SIGNATURE}), public=False),
    "upi_qr": AssetRule(frozenset({Kind.IMAGE}), public=False),
}
SCALAR_FIELDS = (
    "legal_name",
    "trade_name",
    "gstin",
    "pan",
    "cin",
    "email",
    "phone_e164",
    "website",
    "address_line",
    "address_locality",
    "address_region",
    "postal_code",
    "country_code",
    "trust_stats",
    "social",
    "default_meta_description",
    "lead_notification_emails",
    "application_notification_emails",
    "notify_on_new_lead",
    "notify_on_new_application",
    "careers_accepting_general_applications",
    "careers_intro",
    "blog_revalidate_url",
    "quotation_offer_enabled",
    "quotation_offer_title",
    "quotation_offer_description",
    "quotation_offer_details",
    "quotation_offer_title_ml",
    "quotation_offer_description_ml",
    "quotation_offer_details_ml",
    "quotation_offer_valid_from",
    "quotation_offer_valid_until",
    "quotation_offer_image_url",
)
EDITABLE_FIELDS = (*SCALAR_FIELDS, *ASSET_RULES, SECRET_FIELD)
SNAPSHOT_FIELDS = (*SCALAR_FIELDS, *ASSET_RULES)
PROFILE_RELATED = tuple(ASSET_RULES)


def get_profile() -> CompanyProfile | None:
    return CompanyProfile.objects.select_related(*PROFILE_RELATED).first()


def current_profile() -> CompanyProfile:
    """The stored profile, or an unsaved one with the defaults. Staff and website reads never trigger a write."""
    return get_profile() or CompanyProfile()


public_profile = current_profile  # the website payload reads the same singleton


@transaction.atomic
def ensure_profile(user) -> CompanyProfile:
    """The stored singleton, created on first need and audited as ``company.profile_created``. Never call it on a read."""
    profile = get_profile()
    if profile is not None:
        return profile
    try:
        with transaction.atomic():
            profile = CompanyProfile()
            stamp_create(profile, user)
            profile.save()
    except IntegrityError:
        return get_profile()  # created concurrently (singleton unique index); that request audited it
    record("company.profile_created", obj=profile, actor=user, after=profile_snapshot(profile))
    bump(CACHE_NAMESPACE)
    return get_profile()


def profile_snapshot(profile: CompanyProfile) -> dict:
    data = snapshot(profile, SNAPSHOT_FIELDS)
    data["blog_revalidate_secret_set"] = bool(profile.blog_revalidate_secret)
    return data


def _check_asset(field: str, asset: MediaAsset | None) -> str | None:
    if asset is None:
        return None
    rule = ASSET_RULES[field]
    if asset.deleted_at is not None:
        return "The file has been deleted."
    if asset.kind not in rule.kinds:
        return f"Must be a {' or '.join(sorted(rule.kinds))} file."
    if rule.public and not asset.is_public:
        return "Must be a public file (it is shown on the website)."
    return None


def _validate(profile: CompanyProfile, values: dict) -> None:
    errors: dict[str, list[str]] = {}
    for field in ASSET_RULES:
        if field in values:
            problem = _check_asset(field, values[field])
            if problem:
                errors[field] = [problem]
    valid_from = values.get("quotation_offer_valid_from", profile.quotation_offer_valid_from)
    valid_until = values.get("quotation_offer_valid_until", profile.quotation_offer_valid_until)
    if valid_from and valid_until and valid_from > valid_until:
        errors["quotation_offer_valid_until"] = ["The offer ends before it starts."]
    if errors:
        raise DomainError("validation_error", "The company profile is invalid.", errors=errors)


def _locked_profile() -> CompanyProfile | None:
    return CompanyProfile.objects.select_for_update().first()


def _changed_values(profile: CompanyProfile, data: dict) -> dict:
    values = {}
    for name, value in data.items():
        if name == SECRET_FIELD:
            value = value or None
        if getattr(profile, name) != value:
            values[name] = value
    return values


@transaction.atomic
def update_profile(*, user, data: dict, expected_version=None) -> CompanyProfile:
    unknown = sorted(set(data) - set(EDITABLE_FIELDS))
    if unknown:
        raise DomainError("validation_error", "Unknown profile fields.", errors={name: ["Not an editable field."] for name in unknown})
    profile = _locked_profile()
    if profile is None:
        # No row yet: clients saw the unsaved defaults as version 1. An edit that changes nothing writes nothing;
        # otherwise the row is created (audited) and edited, so the result is version 2 like any later edit.
        defaults = CompanyProfile()
        check_version(defaults, expected_version)
        _validate(defaults, data)
        if not _changed_values(defaults, data):
            return defaults
        ensure_profile(user)
        profile = _locked_profile()
    check_version(profile, expected_version)  # a concurrent first edit may have moved it on
    _validate(profile, data)
    values = _changed_values(profile, data)
    if not values:
        return get_profile()
    before = profile_snapshot(profile)
    profile.versioned_update(user, **values)
    after = profile_snapshot(profile)
    changed_before, changed_after = changes(before, after)
    record("company.profile_updated", obj=profile, actor=user, before=changed_before, after=changed_after)
    bump(CACHE_NAMESPACE)
    emit("company.profile_updated", {"profile_uid": str(profile.uid), "fields": sorted(values)}, aggregate_type="company.companyprofile", aggregate_uid=profile.uid)
    return get_profile()


@dataclass(frozen=True)
class QuotationOffer:
    enabled: bool
    active: bool
    title: str
    description: str
    details: str
    title_ml: str
    description_ml: str
    details_ml: str
    valid_from: dt.date | None
    valid_until: dt.date | None
    image_src: str


def offer_is_active(profile: CompanyProfile, today: dt.date) -> bool:
    """Printed only while enabled, titled, and ``today`` falls inside the (optional) dates — legacy semantics."""
    if not profile.quotation_offer_enabled or not profile.quotation_offer_title.strip():
        return False
    if profile.quotation_offer_valid_from and profile.quotation_offer_valid_from > today:
        return False
    if profile.quotation_offer_valid_until and profile.quotation_offer_valid_until < today:
        return False
    return True


def quotation_offer(profile: CompanyProfile, today: dt.date | None = None) -> QuotationOffer:
    today = today or timezone.localdate()
    image = profile.quotation_offer_image
    image_src = image.cdn_url if image is not None and image.deleted_at is None and image.is_public and image.cdn_url else profile.quotation_offer_image_url
    return QuotationOffer(
        enabled=profile.quotation_offer_enabled,
        active=offer_is_active(profile, today),
        title=profile.quotation_offer_title,
        description=profile.quotation_offer_description,
        details=profile.quotation_offer_details,
        # Malayalam quotations fall back to the English text when these are blank (legacy behaviour).
        title_ml=profile.quotation_offer_title_ml or profile.quotation_offer_title,
        description_ml=profile.quotation_offer_description_ml or profile.quotation_offer_description,
        details_ml=profile.quotation_offer_details_ml or profile.quotation_offer_details,
        valid_from=profile.quotation_offer_valid_from,
        valid_until=profile.quotation_offer_valid_until,
        image_src=image_src,
    )
