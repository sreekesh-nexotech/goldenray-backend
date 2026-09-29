"""``pricing_offer`` + ``pricing_offer_transition`` (PLAN §2.3; lifecycle DV-19).

Standing campaign reductions from the pre-GST list selling price. The lifecycle is Flarize's (``engines.offers``:
DRAFT → APPROVED → ACTIVE → EXPIRED → ARCHIVED, APPROVED/EXPIRED → DRAFT, anything → ARCHIVED) plus PLAN's PAUSED
(ACTIVE ⇄ PAUSED). Every transition is a row of ``pricing_offer_transition``.

Columns beyond the PLAN list (lossless home for ``bom.Offer`` and Flarize ``catalog.json`` offers; DV): ``code`` is 50
characters (``bom.Offer.offer_id``); ``applies_to_system`` includes UPGRADE (``bom.Offer.applies_to``);
``applies_to_size_key`` keeps the Flarize size key (``5sp`` / ``5tp`` — ``applies_to_size_kw`` alone cannot tell 5 kW
1P from 3P); ``description`` and ``content_version`` (Flarize ``offerVersion``); ``status_changed_at``.
"""

from django.conf import settings
from django.db import models
from django.db.models import F, Q
from django.db.models.functions import Lower
from django.utils import timezone

from core.models import BaseModel
from pricing.models.choices import LIVE, OfferStatus, OfferSystem, OfferTier, OfferType, in_choices


class Offer(BaseModel):
    code = models.CharField(max_length=50)
    name = models.CharField(max_length=120)
    type = models.CharField(max_length=8, choices=OfferType.choices)
    value = models.DecimalField(max_digits=12, decimal_places=2)
    applies_to_system = models.CharField(max_length=8, choices=OfferSystem.choices, default=OfferSystem.ALL)
    applies_to_tier = models.CharField(max_length=8, choices=OfferTier.choices, default=OfferTier.ALL)
    applies_to_size_kw = models.DecimalField(max_digits=6, decimal_places=2, null=True, blank=True)
    applies_to_size_key = models.CharField(max_length=8, blank=True, default="", help_text="Size key (3, 5sp, 5tp …); blank = every size.")
    starts_on = models.DateField(null=True, blank=True)
    ends_on = models.DateField(null=True, blank=True)
    status = models.CharField(max_length=10, choices=OfferStatus.choices, default=OfferStatus.DRAFT)
    status_changed_at = models.DateTimeField(null=True, blank=True)
    print_on_quotation = models.BooleanField(default=True)
    stackable = models.BooleanField(default=False)
    description = models.TextField(blank=True, default="")
    content_version = models.PositiveIntegerField(default=1, help_text="Bumped on every content change (Flarize offerVersion).")

    class Meta:
        db_table = "pricing_offer"
        ordering = ["-created_at", "-id"]
        constraints = [
            models.UniqueConstraint(Lower("code"), condition=LIVE, name="pricing_offer_code_live_uniq"),
            models.CheckConstraint(condition=in_choices("type", OfferType), name="pricing_offer_type_valid"),
            models.CheckConstraint(condition=in_choices("status", OfferStatus), name="pricing_offer_status_valid"),
            models.CheckConstraint(condition=in_choices("applies_to_system", OfferSystem), name="pricing_offer_system_valid"),
            models.CheckConstraint(condition=in_choices("applies_to_tier", OfferTier), name="pricing_offer_tier_valid"),
            models.CheckConstraint(condition=Q(value__gte=0), name="pricing_offer_value_not_negative"),
            models.CheckConstraint(condition=~Q(type=OfferType.PERCENT) | Q(value__lte=100), name="pricing_offer_percent_at_most_100"),
            models.CheckConstraint(condition=Q(starts_on__isnull=True) | Q(ends_on__isnull=True) | Q(ends_on__gte=F("starts_on")), name="pricing_offer_date_order"),
            models.CheckConstraint(condition=Q(applies_to_size_kw__isnull=True) | Q(applies_to_size_kw__gt=0), name="pricing_offer_size_positive"),
            models.CheckConstraint(condition=Q(code__regex=r"^[A-Za-z0-9][A-Za-z0-9_.-]{0,49}$"), name="pricing_offer_code_format"),
            models.CheckConstraint(condition=~Q(name=""), name="pricing_offer_name_not_blank"),
        ]
        indexes = [models.Index(fields=["status", "starts_on", "ends_on"], name="pricing_offer_status_dates")]

    def __str__(self) -> str:
        return f"{self.code} — {self.name} ({self.status})"


class OfferTransition(models.Model):
    """*(no base)* One lifecycle step of an offer (who, when, why)."""

    id = models.BigAutoField(primary_key=True)
    # CASCADE: the history belongs to the offer (offers are only soft-deleted in practice).
    offer = models.ForeignKey(Offer, on_delete=models.CASCADE, related_name="transitions")
    from_status = models.CharField(max_length=10, blank=True, default="", help_text="Blank for the creation step.")
    to_status = models.CharField(max_length=10)
    at = models.DateTimeField(default=timezone.now)
    # SET_NULL: attribution only.
    by = models.ForeignKey(settings.AUTH_USER_MODEL, null=True, blank=True, on_delete=models.SET_NULL, related_name="+")
    by_label = models.CharField(max_length=64, blank=True, default="", help_text="Source actor id when it is not a platform user (Flarize admin-001, SYSTEM).")
    reason = models.TextField(blank=True, default="")

    class Meta:
        db_table = "pricing_offer_transition"
        ordering = ["offer_id", "at", "id"]
        constraints = [
            models.CheckConstraint(condition=Q(from_status="") | in_choices("from_status", OfferStatus), name="pricing_offer_transition_from_valid"),
            models.CheckConstraint(condition=in_choices("to_status", OfferStatus), name="pricing_offer_transition_to_valid"),
        ]
        indexes = [models.Index(fields=["offer", "at"], name="pricing_offer_tr_offer_at")]

    def __str__(self) -> str:
        return f"{self.offer_id}: {self.from_status or '∅'} → {self.to_status}"
