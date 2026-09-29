"""``agreements_agreement`` and ``agreements_line`` (PLAN §2.6; Plan 2 §3.1; DV-2, DV-3).

An agreement is one signed commitment of a customer: a Purchase Agreement or Sale Order derived from an issued
quotation version (values pinned, prices never typed), or a blank Sale Order / Extra Structure agreement (an Extra
Structure one priced line by line, raised from a site inspection's additional work: ``source_type``/``source_uid``,
DV-3). A DRAFT is edited; ``issue`` takes the number (``core.sequences`` AGR), deep-freezes the document ``payload``
(+ SHA-256) and renders it; an issued agreement is never edited again — it is superseded by a new revision (a new row,
``supersedes`` → the old one) or cancelled. Imported Purchase Agreement page records are ISSUED rows with
``legacy = true`` and the raw browser record in ``payload`` (PLAN §7.5).
"""

from __future__ import annotations

from django.conf import settings
from django.db import models
from django.db.models import F, Q

from agreements.models.choices import AcceptedVia, AgreementKind, AgreementStatus, InverterType, Language, Phase, SourceType, SystemType, Variant, in_choices
from core.models import BaseModel

LIVE = Q(deleted_at__isnull=True)
SHA256_RE = r"^[0-9a-f]{64}$"
FROZEN = ("ISSUED", "ACCEPTED", "SUPERSEDED")
IN_FORCE = ("ISSUED", "ACCEPTED")


class Agreement(BaseModel):
    number = models.CharField(max_length=48, blank=True, default="", help_text="AGR-<FY>-<nnnn> (core.sequences AGR), taken at issue; blank while DRAFT. Imported rows: <PROFILE>-<record id>.")
    kind = models.CharField(max_length=20, choices=AgreementKind.choices)
    # PROTECT: a customer with agreements is merged, never deleted (customers.services.merge re-points this column).
    customer = models.ForeignKey("customers.Customer", on_delete=models.PROTECT, related_name="agreements")
    # SET_NULL: record-scope anchor (agreements `owned`, DV-135); deleting a user never deletes agreements.
    owner = models.ForeignKey(settings.AUTH_USER_MODEL, null=True, blank=True, on_delete=models.SET_NULL, related_name="+")
    # PROTECT: the issued quotation version whose values this agreement pins (DV-2: the link lives on this side).
    quotation_version = models.ForeignKey("quotations.Version", null=True, blank=True, on_delete=models.PROTECT, related_name="agreements")
    source_type = models.CharField(max_length=20, choices=SourceType.choices, blank=True, default="", help_text="DV-3: what a blank agreement was raised from.")
    source_uid = models.UUIDField(null=True, blank=True, help_text="DV-3: the uid of that record (e.g. the site inspection); no foreign key.")
    status = models.CharField(max_length=12, choices=AgreementStatus.choices, default=AgreementStatus.DRAFT)
    revision = models.PositiveSmallIntegerField(default=1, help_text="PLAN `version` (DV-136): 1, then +1 for every superseding agreement of the chain.")
    # SET_NULL: the agreement this revision replaces (the chain survives a soft delete of either row).
    supersedes = models.ForeignKey("self", null=True, blank=True, on_delete=models.SET_NULL, related_name="superseded_by")
    language = models.CharField(max_length=2, choices=Language.choices, default=Language.EN)
    # ── system ──────────────────────────────────────────────────────────────────────────────────────────────────
    system_type = models.CharField(max_length=8, choices=SystemType.choices, default=SystemType.ON_GRID)
    capacity_kw = models.DecimalField(max_digits=6, decimal_places=2, null=True, blank=True)
    size_label = models.CharField(max_length=120, blank=True, default="", help_text="The plant as printed (legacy `kw`, e.g. '5KW Plant with 6KW Inverter – Hybrid').")
    phase = models.CharField(max_length=2, choices=Phase.choices, blank=True, default="")
    variant = models.CharField(max_length=8, choices=Variant.choices, blank=True, default="", help_text="Package tier printed after the price.")
    # ── components (PROTECT: catalog masters; NULL for imported rows, whose labels are kept as printed) ──────────
    panel = models.ForeignKey("catalog.Component", null=True, blank=True, on_delete=models.PROTECT, related_name="+")
    panel_label = models.CharField(max_length=255, blank=True, default="")
    panel_capacity_w = models.PositiveIntegerField(null=True, blank=True)
    panel_capacity_label = models.CharField(max_length=64, blank=True, default="", help_text="Wattage range as printed (legacy `panelcap`, e.g. '545W – 580W').")
    panel_dcr = models.BooleanField(null=True, blank=True)
    panel_qty = models.PositiveIntegerField(null=True, blank=True)
    # PROTECT: catalog master.
    inverter = models.ForeignKey("catalog.Component", null=True, blank=True, on_delete=models.PROTECT, related_name="+")
    inverter_brand = models.CharField(max_length=100, blank=True, default="")
    inverter_type = models.CharField(max_length=8, choices=InverterType.choices, blank=True, default="")
    inverter_qty = models.PositiveIntegerField(null=True, blank=True)
    # PROTECT: catalog master.
    battery = models.ForeignKey("catalog.Component", null=True, blank=True, on_delete=models.PROTECT, related_name="+")
    battery_label = models.CharField(max_length=64, blank=True, default="", help_text="Battery option as printed ('' = None).")
    battery_qty = models.PositiveIntegerField(null=True, blank=True)
    # PROTECT: configuration master (bom_structure_template).
    structure_template = models.ForeignKey("bom.StructureTemplate", null=True, blank=True, on_delete=models.PROTECT, related_name="+")
    structure_type = models.CharField(max_length=24, blank=True, default="", help_text="Roof / installation type (FLAT, SHEET, ELEVATED).")
    structure_material = models.CharField(max_length=120, blank=True, default="")
    extra_structure = models.BooleanField(default=False)
    walkway_required = models.BooleanField(default=False)
    ladder_required = models.BooleanField(default=False)
    # ── prices (pinned from the quotation; changed only by an audited price override) ──────────────────────────
    original_price = models.DecimalField(max_digits=14, decimal_places=2, null=True, blank=True)
    extra_cost = models.DecimalField(max_digits=14, decimal_places=2, default=0)
    discount = models.DecimalField(max_digits=14, decimal_places=2, default=0)
    final_price = models.DecimalField(max_digits=14, decimal_places=2, null=True, blank=True)
    # SET_NULL: the pricing_statutory_fee row the fee was taken from (the amount and label are copied).
    statutory_fee = models.ForeignKey("pricing.StatutoryFee", null=True, blank=True, on_delete=models.SET_NULL, related_name="+")
    statutory_fee_label = models.CharField(max_length=120, blank=True, default="")
    statutory_fee_amount = models.DecimalField(max_digits=14, decimal_places=2, null=True, blank=True)
    add_on_offer = models.TextField(blank=True, default="")
    extra_description = models.TextField(blank=True, default="")
    price_override_reason = models.TextField(blank=True, default="")
    # ── engineer-verifiable site facts handed to the inspection (agreements.issued `fields`) ──────────────────────
    consumer_number = models.CharField(max_length=20, blank=True, default="", help_text="KSEB consumer number.")
    registered_phone_e164 = models.CharField(max_length=16, blank=True, default="", help_text="Phone registered with KSEB (E.164).")
    wheeling_required = models.BooleanField(null=True, blank=True)
    # ── the frozen document ─────────────────────────────────────────────────────────────────────────────────────
    payload = models.JSONField(null=True, blank=True, help_text="The deep-frozen document, written once at issue (imported rows: + the raw browser record).")
    payload_sha256 = models.CharField(max_length=64, blank=True, default="")
    # SET_NULL: the PDF rendered at issue; the frozen payload is the source of truth, a job can be re-run.
    document_job = models.ForeignKey("documents.RenderJob", null=True, blank=True, on_delete=models.SET_NULL, related_name="+")
    issued_at = models.DateTimeField(null=True, blank=True)
    # SET_NULL: attribution only.
    issued_by = models.ForeignKey(settings.AUTH_USER_MODEL, null=True, blank=True, on_delete=models.SET_NULL, related_name="+")
    accepted_at = models.DateTimeField(null=True, blank=True)
    accepted_via = models.CharField(max_length=8, choices=AcceptedVia.choices, blank=True, default="")
    # SET_NULL: the private scan of the paper-signed copy (media.usage refuses to delete it while referenced).
    acceptance_asset = models.ForeignKey("media.MediaAsset", null=True, blank=True, on_delete=models.SET_NULL, related_name="+")
    acceptance_note = models.TextField(blank=True, default="")
    # SET_NULL: attribution only (who recorded the customer's acceptance).
    acceptance_recorded_by = models.ForeignKey(settings.AUTH_USER_MODEL, null=True, blank=True, on_delete=models.SET_NULL, related_name="+")
    superseded_at = models.DateTimeField(null=True, blank=True)
    cancelled_at = models.DateTimeField(null=True, blank=True)
    cancel_reason = models.TextField(blank=True, default="")
    legacy = models.BooleanField(default=False, help_text="Imported from the Purchase Agreement page (flarize_agr).")
    legacy_ref = models.CharField(max_length=96, blank=True, default="", help_text="<profile>/<record id> of an imported record.")
    legacy_quotation_ref = models.CharField(max_length=48, blank=True, default="", help_text="Quotation number typed on the legacy page (QUO-GR-AS-26-…).")

    class Meta:
        db_table = "agreements_agreement"
        ordering = ["-created_at", "-id"]
        constraints = [
            models.UniqueConstraint(fields=["number"], condition=LIVE & ~Q(number=""), name="agreements_agreement_number_uniq"),
            models.UniqueConstraint(fields=["legacy_ref"], condition=LIVE & ~Q(legacy_ref=""), name="agreements_agreement_legacy_uniq"),
            # PLAN's PU (one ISSUED per version and kind), widened to ACCEPTED: one agreement in force (DV-138).
            models.UniqueConstraint(fields=["quotation_version", "kind"], condition=LIVE & Q(status__in=IN_FORCE), name="agreements_agreement_one_in_force"),
            models.UniqueConstraint(fields=["quotation_version", "kind"], condition=LIVE & Q(status="DRAFT"), name="agreements_agreement_one_draft"),
            models.UniqueConstraint(fields=["supersedes"], condition=LIVE & ~Q(status="CANCELLED"), name="agreements_agreement_superseded_once"),
            models.CheckConstraint(condition=in_choices("kind", AgreementKind), name="agreements_agreement_kind_valid"),
            models.CheckConstraint(condition=in_choices("status", AgreementStatus), name="agreements_agreement_status_valid"),
            models.CheckConstraint(condition=in_choices("language", Language), name="agreements_agreement_language_valid"),
            models.CheckConstraint(condition=in_choices("system_type", SystemType), name="agreements_agreement_system_type_valid"),
            models.CheckConstraint(condition=in_choices("phase", Phase, blank=True), name="agreements_agreement_phase_valid"),
            models.CheckConstraint(condition=in_choices("variant", Variant, blank=True), name="agreements_agreement_variant_valid"),
            models.CheckConstraint(condition=in_choices("inverter_type", InverterType, blank=True), name="agreements_agreement_inverter_type_valid"),
            models.CheckConstraint(condition=in_choices("source_type", SourceType, blank=True), name="agreements_agreement_source_type_valid"),
            models.CheckConstraint(condition=in_choices("accepted_via", AcceptedVia, blank=True), name="agreements_agreement_accepted_via_valid"),
            models.CheckConstraint(condition=Q(source_type="", source_uid__isnull=True) | (~Q(source_type="") & Q(source_uid__isnull=False)), name="agreements_agreement_source_pair"),
            models.CheckConstraint(condition=Q(source_type="") | Q(quotation_version__isnull=True), name="agreements_agreement_source_or_quotation"),
            models.CheckConstraint(condition=Q(legacy=False) | Q(quotation_version__isnull=True), name="agreements_agreement_legacy_no_quotation"),
            models.CheckConstraint(condition=Q(revision__gte=1), name="agreements_agreement_revision_positive"),
            models.CheckConstraint(condition=Q(capacity_kw__isnull=True) | Q(capacity_kw__gt=0), name="agreements_agreement_capacity_positive"),
            models.CheckConstraint(
                condition=(Q(original_price__isnull=True) | Q(original_price__gte=0))
                & Q(extra_cost__gte=0)
                & Q(discount__gte=0)
                & (Q(final_price__isnull=True) | Q(final_price__gte=0))
                & (Q(statutory_fee_amount__isnull=True) | Q(statutory_fee_amount__gte=0)),
                name="agreements_agreement_money_not_negative",
            ),
            models.CheckConstraint(
                condition=Q(legacy=True) | Q(final_price=F("original_price") + F("extra_cost") - F("discount")) | Q(final_price__isnull=True) | Q(original_price__isnull=True),
                name="agreements_agreement_price_arithmetic",
            ),
            models.CheckConstraint(
                condition=~Q(status__in=FROZEN) | (~Q(number="") & Q(payload__isnull=False, payload_sha256__regex=SHA256_RE, issued_at__isnull=False)),
                name="agreements_agreement_issued_is_frozen",
            ),
            models.CheckConstraint(condition=~Q(status="DRAFT") | Q(number="", payload__isnull=True, payload_sha256=""), name="agreements_agreement_draft_not_frozen"),
            models.CheckConstraint(condition=~Q(status="ACCEPTED") | (Q(accepted_at__isnull=False) & ~Q(accepted_via="")), name="agreements_agreement_accepted_recorded"),
            models.CheckConstraint(condition=~Q(status="CANCELLED") | Q(cancelled_at__isnull=False), name="agreements_agreement_cancelled_at"),
        ]
        indexes = [
            models.Index(fields=["owner", "-created_at"], name="agreements_agreement_owner"),
            models.Index(fields=["customer", "-created_at"], name="agreements_agreement_customer"),
            models.Index(fields=["status", "kind"], name="agreements_agreement_status"),
            models.Index(fields=["source_type", "source_uid"], name="agreements_agreement_source"),
        ]

    def __str__(self) -> str:
        return self.number or f"Agreement {self.uid} ({self.status})"


class AgreementLine(BaseModel):
    # CASCADE: a line is a true child of its (Extra Structure) agreement.
    agreement = models.ForeignKey(Agreement, on_delete=models.CASCADE, related_name="lines")
    description = models.CharField(max_length=255)
    quantity = models.DecimalField(max_digits=10, decimal_places=2)
    unit = models.CharField(max_length=12, blank=True, default="")
    unit_price = models.DecimalField(max_digits=14, decimal_places=2)
    amount = models.DecimalField(max_digits=14, decimal_places=2)
    additional_work_item_uid = models.UUIDField(null=True, blank=True, help_text="DV-3: the site-inspection additional-work item this line prices (no foreign key).")
    sort_order = models.PositiveSmallIntegerField(default=0)

    class Meta:
        db_table = "agreements_line"
        ordering = ["agreement_id", "sort_order", "id"]
        constraints = [
            models.CheckConstraint(condition=~Q(description=""), name="agreements_line_description_present"),
            models.CheckConstraint(condition=Q(quantity__gt=0), name="agreements_line_quantity_positive"),
            models.CheckConstraint(condition=Q(unit_price__gte=0) & Q(amount__gte=0), name="agreements_line_money_not_negative"),
        ]

    def __str__(self) -> str:
        return f"{self.description} × {self.quantity}"
