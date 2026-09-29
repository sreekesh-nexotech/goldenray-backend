"""Legacy import of the company settings singletons (mapping tables in docs/decisions/foundation-f3.md, DV-11).

* :func:`import_site_settings` — CMS ``siteconfig_settings`` (PLAN §7.2 row 11) → ``company_profile``: company name,
  e-mail, phone (E.164), address, notification lists and switches, careers settings. The SEO defaults of the same row
  (``default_meta_description``, ``default_og_image_id``) are ``seo.services.legacy_import.import_site_seo_defaults``.
* :func:`import_quotation_settings` — main backend ``bom_quotationsettings`` (PLAN §7.3) → the ``quotation_offer_*``
  fields; the uploaded ``offer_image`` file is read with ``read_file(path)`` and stored through the media upload
  pipeline (sniffed, public IMAGE, folder ``company``).

Both take the singleton's rows (plain dicts, ``SELECT *``) and return ``{"created", "updated", "skipped",
"violations"}``. They write through ``company.services.profile.update_profile`` (validation, audit, cache, outbox), so
re-running changes nothing when the source did not change. Empty source texts are **not** written (an empty CMS field
means "not set", and must not blank a value another import or Studio already set); booleans and dates always are.
The row is mapped in ``core_legacy_map`` (``CMS``/``siteconfig_settings``, ``BACKEND``/``bom_quotationsettings``).
``dry_run=True`` rolls back and uploads nothing. Each call writes one ``company.legacy_import`` audit row.
"""

from __future__ import annotations

import hashlib
import io
import json
from collections.abc import Callable, Iterable

import phonenumbers
from django.core.serializers.json import DjangoJSONEncoder
from django.core.validators import validate_email
from django.db import transaction
from django.utils.dateparse import parse_date

from audit.services import record
from company.services.profile import current_profile, update_profile
from core.errors import DomainError
from core.models import LegacyMap
from media.models import MediaAsset
from media.services import assets as media_assets

CMS = LegacyMap.SourceSystem.CMS
BACKEND = LegacyMap.SourceSystem.BACKEND
SITE_TABLE = "siteconfig_settings"
QUOTATION_TABLE = "bom_quotationsettings"
OFFER_IMAGE_TABLE = "bom_quotationsettings.offer_image"
TEXT_FIELDS = {
    "company_name": "trade_name",
    "company_email": "email",
    "address_line": "address_line",
    "address_locality": "address_locality",
    "address_region": "address_region",
    "postal_code": "postal_code",
    "careers_intro": "careers_intro",
}
BOOL_FIELDS = {
    "notify_on_new_lead": "notify_on_new_lead",
    "notify_on_new_application": "notify_on_new_application",
    "careers_accepting_general_applications": "careers_accepting_general_applications",
}
OFFER_TEXTS = ("offer_title", "offer_description", "offer_details", "offer_title_ml", "offer_description_ml", "offer_details_ml", "offer_image_url")


class Report:
    def __init__(self, source_table: str):
        self.source_table = source_table
        self.created = self.updated = self.skipped = 0
        self.violations: list[dict] = []

    def violation(self, source_id, code: str, message: str) -> None:
        self.violations.append({"source_table": self.source_table, "source_id": str(source_id), "code": code, "message": message})

    def as_dict(self) -> dict:
        return {"created": self.created, "updated": self.updated, "skipped": self.skipped, "violations": self.violations}


def checksum(rows: list[dict]) -> str:
    return hashlib.sha256(json.dumps(rows, cls=DjangoJSONEncoder, sort_keys=True, default=str).encode()).hexdigest()


def _emails(value, report: Report, source_id, column: str) -> list[str]:
    result = []
    for part in str(value or "").replace(";", ",").split(","):
        email = part.strip().lower()
        if not email:
            continue
        try:
            validate_email(email)
        except Exception:  # noqa: BLE001 - ValidationError: listed, dropped
            report.violation(source_id, "email_invalid", f"{column}: {email!r} is not an e-mail address; dropped.")
            continue
        if email not in result:
            result.append(email)
    return result


def _phone(value, report: Report, source_id) -> str:
    raw = str(value or "").strip()
    if not raw:
        return ""
    try:
        number = phonenumbers.parse(raw, "IN")
    except phonenumbers.NumberParseException:
        number = None
    if number is None or not phonenumbers.is_valid_number(number):
        report.violation(source_id, "phone_invalid", f"company_phone {raw!r} is not a valid number; left blank.")
        return ""
    return phonenumbers.format_number(number, phonenumbers.PhoneNumberFormat.E164)


def _apply(report: Report, data: dict, *, user, source_system: str, source_id) -> None:
    before = current_profile()
    changed = {name: value for name, value in data.items() if getattr(before, name) != value}
    if not changed:
        report.skipped += 1
    else:
        was_saved = before.pk is not None
        try:
            update_profile(user=user, data=changed)
        except DomainError as exc:
            for field, messages in (exc.errors or {}).items():
                report.violation(source_id, "invalid_value", f"{field}: {' '.join(messages)}")
            report.skipped += 1
            return
        report.updated += was_saved
        report.created += not was_saved
    profile = current_profile()
    if profile.pk is not None:
        LegacyMap.objects.update_or_create(
            source_system=source_system, source_table=report.source_table, source_id=str(source_id), defaults={"target_table": profile._meta.db_table, "target_id": profile.pk}
        )


def _finish(report: Report, rows: list[dict], *, user, dry_run: bool) -> dict:
    record(
        "company.legacy_import",
        object_type="company.legacyimport",
        actor=user,
        actor_kind=None if user else "SYSTEM",
        after={"source_table": report.source_table, "rows": len(rows), "checksum": checksum(rows), **{**report.as_dict(), "violations": len(report.violations)}},
    )
    if dry_run:
        transaction.set_rollback(True)
    return report.as_dict()


@transaction.atomic
def import_site_settings(rows: Iterable[dict], *, site_url: str = "", user=None, dry_run: bool = False) -> dict:
    """CMS ``siteconfig_settings`` (the singleton; only the first row counts) → company profile.

    ``site_url`` is the legacy CMS ``FRONTEND_BASE_URL`` (an environment setting, not a column): the CMS printed it as
    the site URL of the job-posting and page schemas, which the platform reads from ``company_profile.website`` — so it
    fills ``website`` while that is empty.
    """
    rows = list(rows)
    report = Report(SITE_TABLE)
    for index, row in enumerate(rows):
        source_id = row.get("id", 1)
        if index:
            report.violation(source_id, "not_singleton", "only the first settings row is imported.")
            report.skipped += 1
            continue
        data = {target: str(row.get(column) or "").strip() for column, target in TEXT_FIELDS.items() if str(row.get(column) or "").strip()}
        if site_url and not current_profile().website:
            data["website"] = site_url.rstrip("/")
        if "trade_name" in data and not current_profile().legal_name:
            data["legal_name"] = data["trade_name"]
        if "email" in data:
            data["email"] = _emails(data["email"], report, source_id, "company_email")[:1]
            data["email"] = data["email"][0] if data["email"] else ""
            if not data["email"]:
                data.pop("email")
        phone = _phone(row.get("company_phone"), report, source_id)
        if phone:
            data["phone_e164"] = phone
        country = str(row.get("country_code") or "").strip().upper()
        if country:
            if len(country) == 2 and country.isalpha():
                data["country_code"] = country
            else:
                report.violation(source_id, "country_invalid", f"country_code {country!r} is not ISO alpha-2; not imported.")
        for column, target in (("lead_notification_emails", "lead_notification_emails"), ("application_notification_emails", "application_notification_emails")):
            if str(row.get(column) or "").strip():
                data[target] = _emails(row.get(column), report, source_id, column)
        for column, target in BOOL_FIELDS.items():
            if row.get(column) is not None:
                data[target] = bool(row.get(column))
        _apply(report, data, user=user, source_system=CMS, source_id=source_id)
    return _finish(report, rows, user=user, dry_run=dry_run)


def _offer_image(path: str, read_file, report: Report, source_id, *, user, dry_run: bool) -> MediaAsset | None | bool:
    """The uploaded offer image as a public asset; ``False`` when it cannot be imported (the field is left alone)."""
    data = read_file(path) if read_file is not None else None
    if data is None:
        report.violation(source_id, "file_unavailable", f"offer_image {path!r} is not in the legacy media volume; not imported.")
        return False
    digest = hashlib.sha256(data).hexdigest()
    source_key = f"{digest}:{path}"
    mapped = LegacyMap.objects.filter(source_system=BACKEND, source_table=OFFER_IMAGE_TABLE, source_id=source_key[:128]).values_list("target_id", flat=True).first()
    asset = MediaAsset.objects.filter(pk=mapped).first() if mapped else None
    if asset is not None:
        return asset  # the same bytes were uploaded by an earlier run (GPS stripping may have changed the stored copy)
    if dry_run:
        report.violation(source_id, "upload_pending", f"offer_image {path!r} would be uploaded.")
        return False
    buffer = io.BytesIO(data)
    buffer.size = len(data)
    try:
        asset = media_assets.upload(user=user, file=buffer, visibility=MediaAsset.Visibility.PUBLIC, kind=MediaAsset.Kind.IMAGE, folder="company", original_filename=path.rsplit("/", 1)[-1])
    except DomainError as exc:
        report.violation(source_id, "file_refused", f"offer_image {path!r} refused ({exc.code}); not imported.")
        return False
    LegacyMap.objects.update_or_create(source_system=BACKEND, source_table=OFFER_IMAGE_TABLE, source_id=source_key[:128], defaults={"target_table": asset._meta.db_table, "target_id": asset.pk})
    return asset


@transaction.atomic
def import_quotation_settings(rows: Iterable[dict], *, read_file: Callable[[str], bytes | None] | None = None, user=None, dry_run: bool = False) -> dict:
    """Main backend ``bom_quotationsettings`` (singleton) → the profile's quotation offer fields."""
    rows = list(rows)
    report = Report(QUOTATION_TABLE)
    for index, row in enumerate(rows):
        source_id = row.get("id", 1)
        if index:
            report.violation(source_id, "not_singleton", "only the first settings row is imported.")
            report.skipped += 1
            continue
        data = {f"quotation_{column}": str(row.get(column) or "").strip() for column in OFFER_TEXTS if str(row.get(column) or "").strip()}
        data["quotation_offer_enabled"] = bool(row.get("offer_enabled"))
        for column in ("offer_valid_from", "offer_valid_until"):
            value = row.get(column)
            data[f"quotation_{column}"] = parse_date(value) if isinstance(value, str) and value else (value or None)
        if row.get("offer_image"):
            image = _offer_image(str(row["offer_image"]), read_file, report, source_id, user=user, dry_run=dry_run)
            if image is not False:
                data["quotation_offer_image"] = image
        _apply(report, data, user=user, source_system=BACKEND, source_id=source_id)
    return _finish(report, rows, user=user, dry_run=dry_run)
