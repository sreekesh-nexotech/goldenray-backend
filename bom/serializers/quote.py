"""OpenAPI shapes of ``POST /api/public/v1/bom/quote/`` (the legacy ``/bom/api/calculate/`` contract) and of the
staff ``POST bom/build/``.

The quote body is validated by :func:`bom.services.website_quote.parse_request` — the legacy rules, message for
message — so these serializers document the contract and are not used to parse it.
"""

from rest_framework import serializers

QUOTE_STRUCTURE_TYPES = ("flatRoof", "elevated", "sheetRoof")
QUOTE_AMOUNT_TYPES = ("percent", "flat")


class BomQuoteRequestSerializer(serializers.Serializer):
    sys_type = serializers.ChoiceField(choices=["ongrid", "hybrid", "upgrade"])
    size = serializers.CharField(help_text="Size key: 3, 5sp, 5tp, 6, 8, 10 (on-grid); 3, 5, 8, 10 (hybrid); the target kW for upgrades.")
    tier = serializers.ChoiceField(choices=["base", "value", "premium"])
    bat_config = serializers.CharField(required=False, default="0", help_text="Hybrid battery band 0/1/2.")
    mode = serializers.CharField(required=False, default="quotation", help_text="Accepted and ignored (as by the legacy endpoint).")
    dist_km = serializers.IntegerField(required=False, default=100, min_value=0)
    structure_type = serializers.ChoiceField(choices=QUOTE_STRUCTURE_TYPES, required=False, default="flatRoof")
    subsidy_type = serializers.ChoiceField(choices=["residential", "ghs", "none"], required=False, default="none")
    ghs_houses = serializers.IntegerField(required=False, default=1, min_value=1)
    selected_offer_id = serializers.CharField(required=False, allow_null=True)
    custom_discount = serializers.JSONField(required=False, allow_null=True, help_text='{"type": "percent" | "flat", "value": <number>} — a bare number is a 400.')
    margin_type = serializers.ChoiceField(choices=QUOTE_AMOUNT_TYPES, required=False, default="percent")
    margin_val = serializers.FloatField(required=False, default=20)
    upgrade_from_kw = serializers.FloatField(required=False, default=3)
    upgrade_to_kw = serializers.FloatField(required=False, default=5)
    upgrade_sections = serializers.DictField(child=serializers.BooleanField(), required=False, help_text="panels, structure, inverter, hybrid_inv, battery, wiring.")


class BomQuoteLineSerializer(serializers.Serializer):
    pos = serializers.IntegerField()
    name = serializers.CharField()
    brand = serializers.CharField()
    qty = serializers.FloatField()
    unit = serializers.CharField()
    unit_price = serializers.FloatField()
    amount = serializers.FloatField()
    gst_pct = serializers.FloatField()
    gst_amt = serializers.IntegerField()
    section = serializers.CharField()
    is_variable = serializers.BooleanField()


class BomQuoteCostsSerializer(serializers.Serializer):
    install = serializers.IntegerField()
    service = serializers.IntegerField()
    transport = serializers.IntegerField()
    misc = serializers.IntegerField()
    office = serializers.IntegerField()
    structure_base = serializers.IntegerField()
    structure_extra = serializers.IntegerField()
    structure_labor = serializers.IntegerField()
    structure_repair = serializers.IntegerField()
    cost_total = serializers.IntegerField()


class BomQuoteTotalsSerializer(serializers.Serializer):
    material_ex_gst = serializers.IntegerField()
    gst_5 = serializers.IntegerField()
    gst_12 = serializers.IntegerField()
    gst_18 = serializers.IntegerField()
    material_with_gst = serializers.IntegerField()
    subtotal = serializers.IntegerField()
    margin = serializers.IntegerField()
    grand_total = serializers.IntegerField()


class BomQuotePricingSerializer(serializers.Serializer):
    market_rate = serializers.FloatField()
    customer_price = serializers.FloatField()
    discount_amt = serializers.IntegerField()
    discount_label = serializers.CharField()
    final_price = serializers.FloatField()
    subsidy_amt = serializers.FloatField()
    subsidy_label = serializers.CharField()
    price_after_subsidy = serializers.FloatField()


class BomQuoteMetaSerializer(serializers.Serializer):
    system_type = serializers.CharField()
    size = serializers.CharField()
    tier = serializers.CharField()
    bat_config = serializers.CharField()
    is_three_phase = serializers.BooleanField()
    new_panels = serializers.IntegerField(allow_null=True)


class BomQuoteOfferSerializer(serializers.Serializer):
    offer_id = serializers.CharField()
    name = serializers.CharField()
    offer_type = serializers.ChoiceField(choices=QUOTE_AMOUNT_TYPES)
    value = serializers.FloatField()


class BomQuoteSerializer(serializers.Serializer):
    bom_lines = BomQuoteLineSerializer(many=True)
    cost_breakdown = BomQuoteCostsSerializer()
    totals = BomQuoteTotalsSerializer()
    pricing = BomQuotePricingSerializer()
    meta = BomQuoteMetaSerializer()
    available_offers = BomQuoteOfferSerializer(many=True)


class BomQuotePublicSerializer(serializers.Serializer):
    """The public quote (business default B-1): the legacy body without ``cost_breakdown`` and ``totals``."""

    bom_lines = BomQuoteLineSerializer(many=True)
    pricing = BomQuotePricingSerializer()
    meta = BomQuoteMetaSerializer()
    available_offers = BomQuoteOfferSerializer(many=True)


class BomBuildRequestSerializer(serializers.Serializer):
    system_type = serializers.ChoiceField(choices=["ONGRID", "HYBRID"], help_text="The engine builds on-grid and hybrid packs (upgrades are quoted, not built).")
    size = serializers.CharField(max_length=16, help_text="A size key of the template (3, 5sp, 5tp …).")
    tier = serializers.ChoiceField(choices=["BASE", "VALUE", "PREMIUM"])
    phase = serializers.ChoiceField(choices=["1P", "3P"], required=False, allow_null=True, default=None)
    battery_quantity = serializers.IntegerField(min_value=0, max_value=2, required=False, allow_null=True, default=None)
    future_system_size = serializers.CharField(max_length=16, required=False, allow_blank=True, default="")
    structure = serializers.SlugField(max_length=32, required=False, allow_blank=True, default="", help_text="Structure template slug (flat_roof, elevated, sheet_roof).")
    selections = serializers.DictField(child=serializers.CharField(max_length=32), required=False, default=dict, help_text="{slot category: component sku}.")


class BomBuildStructureLineSerializer(serializers.Serializer):
    name = serializers.CharField()
    item_type = serializers.CharField()
    qty = serializers.FloatField()
    weight_kg = serializers.FloatField(allow_null=True)
    unit_price = serializers.FloatField(allow_null=True)


class BomBuildStructureSerializer(serializers.Serializer):
    slug = serializers.CharField()
    tube_kg = serializers.FloatField()
    lines = BomBuildStructureLineSerializer(many=True)


class BomBuildWarningSerializer(serializers.Serializer):
    code = serializers.CharField()
    slot = serializers.CharField()
    message = serializers.CharField()


class BomBuildSerializer(serializers.Serializer):
    lines = serializers.ListField(child=serializers.DictField(), help_text="engines.bom_builder lines (pos, category, label, name, catalogItemId, qty, unitPrice, amount, gst, gstAmt …).")
    totals = serializers.DictField(help_text="matTotal, allGst, sub, grand.")
    profile = serializers.DictField()
    system_config = serializers.DictField()
    engineering = serializers.DictField()
    structure = BomBuildStructureSerializer(allow_null=True)
    warnings = BomBuildWarningSerializer(many=True)
