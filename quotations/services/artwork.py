"""The QuotationV2 artwork's view of a frozen quotation document (D-3: the website's React ``QuotationV2`` /
``QuotationV2Malayalam`` design, fed by the Flarize payload).

The templates (``quotations/templates/documents/quotation/``) never compute: every figure here is read from the
frozen payload (``document.payload`` and its ``alternatives``) and only *formatted* — Indian digit grouping, dates,
ranges. A section the payload marks blocked prints the artwork's "Not available" instead of a number (Flarize F.1:
"a missing value becomes a named blocked field and is never computed or defaulted"). Texts come from the frozen
bilingual content (``content.value.content``) in the rendering language, then from the artwork's own copy below.
"""

from __future__ import annotations

import datetime as dt
from decimal import ROUND_HALF_UP, Decimal, InvalidOperation

TIERS = ("base", "value", "premium")
ROLE_LABELS = {
    "en": {
        "PANEL": "Solar Panels",
        "INVERTER": "Inverter",
        "MICRO_INVERTER": "Micro Inverter",
        "BATTERY": "Battery",
        "STRUCTURE": "Mounting Structure",
        "ACDB": "AC Distribution Box",
        "DCDB": "DC Distribution Box",
        "AC_CABLE": "AC Cable",
        "DC_CABLE": "DC Cable",
        "AC_ISOLATOR": "AC Isolator",
        "EARTHING": "Earthing",
        "PROTECTION": "Lightning Protection",
        "METER": "Net Meter",
        "MONITORING": "Monitoring",
    },
    "ml": {
        "PANEL": "സോളാർ പാനലുകൾ",
        "INVERTER": "ഇൻവെർട്ടർ",
        "MICRO_INVERTER": "മൈക്രോ ഇൻവെർട്ടർ",
        "BATTERY": "ബാറ്ററി",
        "STRUCTURE": "മൗണ്ടിംഗ് സ്ട്രക്ചർ",
        "ACDB": "AC ഡിസ്ട്രിബ്യൂഷൻ ബോക്സ്",
        "DCDB": "DC ഡിസ്ട്രിബ്യൂഷൻ ബോക്സ്",
        "AC_CABLE": "AC കേബിൾ",
        "DC_CABLE": "DC കേബിൾ",
        "AC_ISOLATOR": "AC ഐസൊലേറ്റർ",
        "EARTHING": "എർത്തിംഗ്",
        "PROTECTION": "മിന്നൽ സംരക്ഷണം",
        "METER": "നെറ്റ് മീറ്റർ",
        "MONITORING": "മോണിറ്ററിംഗ്",
    },
}
#: The artwork's own copy (pages 2, 4–8, 12) — the texts the content store does not carry.
COPY = {
    "en": {
        "brand_line": "Powered by Golden Ray",
        "not_available": "Not available",
        "why_title": "Why Choose Flarize?",
        "why_body": "A solar system is one of the smartest investments for your home, and its performance depends on choosing the right installer. "
        "At Flarize, we combine quality workmanship, premium components, transparent pricing and dependable after-sales support.",
        "why_installations": "Successful Installations",
        "why_years": "Years of Industry Experience",
        "why_team": "Certified Installation Team",
        "why_office": "Dedicated Service Office",
        "why_mnre": "MNRE Registered Vendor",
        "options_title": "Choose the Package That Fits Your Home",
        "options_subtitle": "Full system cost, down payment and EMI for each package.",
        "price_before_gst": "Price before GST",
        "gst": "GST",
        "total": "System Cost (incl. GST)",
        "extras": "Transport beyond included km",
        "discount": "Offer / Discount",
        "payable": "You Pay",
        "subsidy": "PM Surya Ghar Subsidy",
        "after_subsidy": "After Subsidy",
        "down_payment": "Down Payment",
        "emi": "EMI",
        "per_month": "/month",
        "years": "years",
        "whats_included": "What's Included",
        "testimonials_title": "Homes That Already Run on Flarize Solar",
        "bill_before": "Bill before",
        "bill_after": "Bill after",
        "saving": "Monthly saving",
        "specs_title": "Technical Specifications",
        "specs_subtitle": "Every component, every specification — total transparency for your peace of mind.",
        "component": "Component",
        "qty": "Qty",
        "savings_title": "Your Savings Over Time",
        "current_bill": "Current KSEB Bill",
        "with_solar": "With Solar",
        "monthly_savings": "Monthly Savings",
        "payback": "Payback",
        "months": "months",
        "lifetime_savings": "25-Year Savings",
        "emi_option": "EMI Option",
        "bank_details": "Bank Details",
        "bank_name": "Bank",
        "account_name": "Account Name",
        "account_number": "Account Number",
        "ifsc": "IFSC",
        "upi": "UPI",
        "summary_title": "Investment Summary",
        "system_size": "System Size",
        "package": "Package",
        "system_cost": "System Cost",
        "your_investment": "Your Investment",
        "refer": "Refer & Earn — recommend Flarize to a friend and earn a reward when they go solar.",
        "per_month_short": "/mo",
        "page": "Page",
    },
    "ml": {
        "brand_line": "Powered by Golden Ray",
        "not_available": "ലഭ്യമല്ല",
        "why_title": "എന്തുകൊണ്ട് Flarize?",
        "why_body": "നിങ്ങളുടെ വീടിനുള്ള ഏറ്റവും മികച്ച നിക്ഷേപങ്ങളിലൊന്നാണ് സോളാർ; അതിന്റെ പ്രകടനം ശരിയായ ഇൻസ്റ്റാളറെ ആശ്രയിച്ചിരിക്കുന്നു. "
        "ഗുണമേന്മയുള്ള ജോലി, മികച്ച ഘടകങ്ങൾ, സുതാര്യമായ വില, വിശ്വസനീയമായ സർവീസ് — ഇതാണ് Flarize.",
        "why_installations": "വിജയകരമായ ഇൻസ്റ്റലേഷനുകൾ",
        "why_years": "വർഷത്തെ പരിചയം",
        "why_team": "സർട്ടിഫൈഡ് ഇൻസ്റ്റലേഷൻ ടീം",
        "why_office": "സർവീസ് ഓഫീസ്",
        "why_mnre": "MNRE രജിസ്റ്റേർഡ് വെണ്ടർ",
        "options_title": "നിങ്ങളുടെ വീടിന് അനുയോജ്യമായ പാക്കേജ്",
        "options_subtitle": "ഓരോ പാക്കേജിന്റെയും ആകെ ചെലവ്, ഡൗൺ പേയ്‌മെന്റ്, EMI.",
        "price_before_gst": "GST-ക്ക് മുമ്പുള്ള വില",
        "gst": "GST",
        "total": "സിസ്റ്റം വില (GST ഉൾപ്പെടെ)",
        "extras": "അധിക ദൂരത്തിനുള്ള ഗതാഗതം",
        "discount": "ഓഫർ / ഡിസ്കൗണ്ട്",
        "payable": "നിങ്ങൾ നൽകേണ്ടത്",
        "subsidy": "PM സൂര്യ ഘർ സബ്‌സിഡി",
        "after_subsidy": "സബ്‌സിഡിക്ക് ശേഷം",
        "down_payment": "ഡൗൺ പേയ്‌മെന്റ്",
        "emi": "EMI",
        "per_month": "/മാസം",
        "years": "വർഷം",
        "whats_included": "ഉൾപ്പെടുന്നവ",
        "testimonials_title": "ഇതിനകം Flarize സോളാറിലേക്ക് മാറിയ വീടുകൾ",
        "bill_before": "മുമ്പത്തെ ബിൽ",
        "bill_after": "ഇപ്പോഴത്തെ ബിൽ",
        "saving": "പ്രതിമാസ ലാഭം",
        "specs_title": "സാങ്കേതിക വിവരങ്ങൾ",
        "specs_subtitle": "ഓരോ ഘടകവും, ഓരോ സ്പെസിഫിക്കേഷനും — പൂർണ്ണ സുതാര്യത.",
        "component": "ഘടകം",
        "qty": "എണ്ണം",
        "savings_title": "കാലക്രമേണ നിങ്ങളുടെ ലാഭം",
        "current_bill": "നിലവിലെ KSEB ബിൽ",
        "with_solar": "സോളാറിനൊപ്പം",
        "monthly_savings": "പ്രതിമാസ ലാഭം",
        "payback": "മുതൽമുടക്ക് തിരികെ",
        "months": "മാസം",
        "lifetime_savings": "25 വർഷത്തെ ലാഭം",
        "emi_option": "EMI സൗകര്യം",
        "bank_details": "ബാങ്ക് വിവരങ്ങൾ",
        "bank_name": "ബാങ്ക്",
        "account_name": "അക്കൗണ്ട് പേര്",
        "account_number": "അക്കൗണ്ട് നമ്പർ",
        "ifsc": "IFSC",
        "upi": "UPI",
        "summary_title": "നിക്ഷേപ സംഗ്രഹം",
        "system_size": "സിസ്റ്റം",
        "package": "പാക്കേജ്",
        "system_cost": "സിസ്റ്റം വില",
        "your_investment": "നിങ്ങളുടെ നിക്ഷേപം",
        "refer": "Refer & Earn — ഒരു സുഹൃത്തിനെ Flarize-ലേക്ക് ശുപാർശ ചെയ്യൂ, അവർ സോളാറിലേക്ക് മാറുമ്പോൾ സമ്മാനം നേടൂ.",
        "per_month_short": "/മാസം",
        "page": "പേജ്",
    },
}


def _number(value) -> Decimal | None:
    if value is None or isinstance(value, bool):
        return None
    try:
        number = Decimal(str(value))
    except (InvalidOperation, ValueError):
        return None
    return number if number.is_finite() else None


def indian(value, places: int = 0) -> str:
    """``toLocaleString('en-IN')``: 1,23,456 (and ``places`` decimals when asked)."""
    number = _number(value)
    if number is None:
        return ""
    quantum = Decimal(1).scaleb(-places)
    number = number.quantize(quantum, rounding=ROUND_HALF_UP)
    sign = "-" if number < 0 else ""
    whole, _, fraction = f"{abs(number):f}".partition(".")
    head, tail = whole[:-3], whole[-3:]
    groups = []
    while len(head) > 2:
        groups.insert(0, head[-2:])
        head = head[:-2]
    if head:
        groups.insert(0, head)
    text = ",".join([*groups, tail]) if groups else tail
    return f"{sign}{text}{'.' + fraction if fraction else ''}"


def rupees(value) -> str:
    text = indian(value)
    return f"₹{text}" if text else ""


def plain_number(value) -> str:
    number = _number(value)
    if number is None:
        return ""
    return f"{number.normalize():f}"


def date_text(value) -> str:
    """``dd/mm/yyyy`` of an ISO timestamp (the day in India)."""
    if not value:
        return ""
    try:
        moment = dt.datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    except ValueError:
        return str(value)
    if moment.tzinfo is not None:
        moment = moment.astimezone(dt.timezone(dt.timedelta(hours=5, minutes=30)))
    return moment.strftime("%d/%m/%Y")


def pick(field, language: str) -> str:
    """``quotationContent.pick``: the language, then English, then any."""
    if isinstance(field, dict):
        for key in (language, "en", *field.keys()):
            value = field.get(key)
            if isinstance(value, str) and value:
                return value
        return ""
    return str(field or "")


def _value(section) -> dict:
    return (section or {}).get("value") or {} if isinstance(section, dict) and section.get("available") else {}


def _labels(content: dict, language: str):
    labels = (content or {}).get("labels") or {}

    def label(key: str, **variables) -> str:
        text = pick(labels.get(key), language)
        for name, value in variables.items():
            text = text.replace("{" + name + "}", str(value))
        return text

    return label


def _tier_column(payload: dict | None, tier: str, copy: dict, language: str) -> dict:
    if not payload:
        return {"tier": tier, "available": False, "name": "", "not_available": copy["not_available"]}
    system = payload.get("system") or {}
    names = system.get("tierColumnNames") or {}
    pricing = (payload.get("pricing") or {}).get("customer") or {}
    subsidy = _value(payload.get("subsidy"))
    finance = _value(payload.get("financing"))
    savings = _value(payload.get("savings"))
    discount = _value((pricing or {}).get("discount")) if isinstance(pricing, dict) else {}
    rows = ((payload.get("bomSummary") or {}).get("rows")) or []

    def brand(role: str) -> str:
        row = next((row for row in rows if row.get("role") == role), None)
        attributes = (row or {}).get("attributes") or {}
        return " ".join(filter(None, [attributes.get("brand"), attributes.get("model")]))

    total = pricing.get("customerTotalIncludingGST")
    payable = discount.get("customerPayable", total)
    return {
        "tier": tier,
        "available": bool((payload.get("pricing") or {}).get("available")),
        "name": (names.get(tier) or {}).get("value") or tier.capitalize(),
        "badge": system.get("recommendedBadge") or "",
        "before_gst": rupees(pricing.get("sellingPriceBeforeGST")),
        "gst": rupees(pricing.get("gstAmount")),
        "gst_rate": plain_number(pricing.get("gstRatePct")),
        "incl_gst": rupees(pricing.get("sellingPriceIncludingGST")),
        "extras": rupees(pricing.get("extrasIncludingGST")) if _number(pricing.get("extrasIncludingGST")) else "",
        "total": rupees(total),
        "discount": rupees(discount.get("totalReduction")) if discount else "",
        "payable": rupees(payable),
        "subsidy": rupees(subsidy.get("totalSubsidy")) if subsidy else "",
        "after_subsidy": rupees(finance.get("postSubsidyInvestment")) if finance else "",
        "down_payment": rupees(finance.get("downPaymentAmount")) if finance else "",
        "emi": rupees(finance.get("monthlyEMI")) if finance else "",
        "emi_rate": plain_number(finance.get("interestRatePct")) if finance else "",
        "emi_years": plain_number(finance.get("tenureYears")) if finance else "",
        "monthly_savings": rupees(savings.get("monthlySavings")) if savings else "",
        "panel": brand("PANEL"),
        "inverter": brand("INVERTER") or brand("MICRO_INVERTER"),
        "not_available": copy["not_available"],
        "specs": [
            {
                "role": ROLE_LABELS[language].get(row.get("role"), row.get("displayCategory") or row.get("role")),
                "brand": (row.get("attributes") or {}).get("brand") or "",
                "model": (row.get("attributes") or {}).get("model") or "",
                "capacity": (row.get("attributes") or {}).get("capacity") or (f"{(row.get('attributes') or {}).get('moduleWatt')} W" if (row.get("attributes") or {}).get("moduleWatt") else ""),
                "qty": plain_number(row.get("quantity")),
            }
            for row in ((payload.get("technicalSpecifications") or {}).get("rows") or [])
        ],
    }


def view_model(document: dict, language: str) -> dict:
    """Everything the 12 pages print, formatted for ``language`` (``en``/``ml``)."""
    language = "ml" if language == "ml" else "en"
    copy = COPY[language]
    payload = (document or {}).get("payload") or {}
    quotation = payload.get("quotation") or {}
    customer = payload.get("customer") or {}
    system = payload.get("system") or {}
    company = _value(payload.get("company"))
    content_section = payload.get("content") or {}
    content = ((content_section.get("value") or {}).get("content")) if content_section.get("available") else {}
    content = content or {}
    derived = ((content_section.get("value") or {}).get("derived")) or {}
    label = _labels(content, language)
    subsidy = _value(payload.get("subsidy"))
    savings = _value(payload.get("savings"))
    finance = _value(payload.get("financing"))
    energy = _value(payload.get("energyProfile"))
    appliances = _value(payload.get("applianceUsage"))
    testimonials = _value(payload.get("testimonials"))
    campaign = _value(payload.get("campaign"))
    inclusions = payload.get("inclusionsByTier") or {}

    by_tier = {system.get("packageTier") or "value": payload}
    for alternative in payload.get("alternatives") or []:
        by_tier[alternative.get("tier")] = alternative.get("payload")
    columns = [_tier_column(by_tier.get(tier), tier, copy, language) for tier in TIERS]
    selected = next((column for column in columns if column["tier"] == system.get("packageTier")), columns[1])

    service_rows = []
    reference = next((inclusions.get(tier) for tier in TIERS if (inclusions.get(tier) or {}).get("serviceMatrix")), None) or {}
    for index, row in enumerate(reference.get("serviceMatrix") or []):
        cells = []
        for tier in TIERS:
            matrix = (inclusions.get(tier) or {}).get("serviceMatrix") or []
            value = matrix[index]["value"] if index < len(matrix) else None
            cells.append("✓" if value is True else "–" if value is False or value is None else str(value))
        service_rows.append({"label": row.get("label"), "cells": cells})

    bill = customer.get("currentBillAmount")
    cycle = customer.get("currentBillCycle") or "monthly"
    terms = content.get("terms") or []
    split = int((derived.get("termsPage10Count") or {}).get(language) or len(terms))
    size = plain_number(system.get("systemSizeKw"))
    homes = company.get("completedInstallations")
    bank_ready = all(company.get(key) for key in ("bankName", "bankAccountNumber", "bankIfsc"))
    first_name = (customer.get("customerName") or customer.get("name") or "").strip().split(" ")[0] if (customer.get("customerName") or customer.get("name")) else ""

    def term(item) -> dict:
        return {"title": pick(item.get("title"), language), "body": pick(item.get("body"), language)}

    def numbered(index: int, item) -> dict:
        return {"number": index + 1, **term(item)}

    return {
        "language": language,
        "copy": copy,
        "number": quotation.get("quotationNumber") or "",
        "version": quotation.get("quotationVersion"),
        "cover": {
            "headline": label("p1.headline"),
            "subline": label("p1.subline"),
            "badge_subsidy": label("p1.badge.subsidy", amount=rupees(subsidy.get("totalSubsidy"))) if subsidy.get("totalSubsidy") else "",
            "badge_subsidy_sub": label("p1.badge.subsidy.sub") if subsidy.get("totalSubsidy") else "",
            "badge_homes": label("p1.badge.homes", count=homes) if homes else "",
            "badge_homes_sub": label("p1.badge.homes.sub") if homes else "",
            "badge_mnre": label("p1.badge.mnre") if company.get("mnreEmpanelled") is not False else "",
            "badge_mnre_sub": label("p1.badge.mnre.sub"),
            "dear": label("p1.dear", name=first_name),
            "letter": [label("p1.letter1"), label("p1.letter2"), label("p1.letter3")],
            "regards": label("p1.regards"),
            "team": label("p1.team"),
            "details_title": label("p1.details.title"),
            "details": [
                (label("p1.f.name"), customer.get("customerName") or customer.get("name") or ""),
                (label("p1.f.address"), customer.get("address") or ""),
                (label("p1.f.pincode"), customer.get("pincode") or ""),
                (label("p1.f.phone"), customer.get("phone") or ""),
                (label("p1.f.bill"), f"{rupees(bill)}/{'bi-monthly' if cycle == 'bimonthly' else 'monthly'}" if bill is not None else ""),
                (label("p1.f.size"), f"{size} kW" if size else ""),
                (label("p1.f.qno"), quotation.get("quotationNumber") or ""),
                (label("p1.f.by"), quotation.get("proposalBy") or ""),
                (label("p1.f.date"), date_text(quotation.get("quotationDate"))),
                (label("p1.f.valid"), date_text(quotation.get("validUntil"))),
                (label("p1.f.gst"), company.get("gstNumber") or ""),
                (label("p1.f.reg"), company.get("companyRegistration") or ""),
            ],
        },
        "company": {
            "name": company.get("companyName") or company.get("brandName") or "Flarize",
            "address": ", ".join(filter(None, [company.get("address"), company.get("city"), company.get("state"), company.get("pincode")])),
            "phone": company.get("phone") or "",
            "email": company.get("email") or "",
            "website": company.get("website") or "",
            "logo": company.get("logoImageUri") or "",
            "installations": indian(homes) if homes else "",
            "years": plain_number(company.get("yearsExperience")),
        },
        "campaign": {
            "available": bool(campaign),
            "headline": campaign.get("headline") or "",
            "subheadline": campaign.get("subheadline") or "",
            "image": campaign.get("heroImage") or "",
            "period": ((campaign.get("period") or {}).get("displayText")) or "",
        },
        "usage": {
            "title": label("p3.title"),
            "subtitle": label("p3.subtitle"),
            "gen_label": label("p3.gen.label"),
            "gen_range": f"{plain_number(energy.get('dailyGenerationLow'))}–{plain_number(energy.get('dailyGenerationHigh'))}" if energy else copy["not_available"],
            "gen_units": label("p3.gen.units"),
            "gen_note": label("p3.gen.note"),
            "value_label": label("p3.value.label"),
            "value_range": f"{rupees(energy.get('monthlyKsebValueLow'))}–{indian(energy.get('monthlyKsebValueHigh'))}" if energy else copy["not_available"],
            "value_note": label("p3.value.note"),
            "table_title": label("p3.table.title"),
            "headers": [label("p3.th.appliance"), label("p3.th.qty"), label("p3.th.hours"), label("p3.th.units")],
            "rows": [
                {
                    "icon": row.get("icon") or "",
                    "name": pick(row.get("name"), language),
                    "qty": plain_number(row.get("qty")),
                    "hours": plain_number(row.get("hours")),
                    "units": plain_number(row.get("units")),
                }
                for row in appliances.get("appliances") or []
            ],
            "total_label": label("p3.total"),
            "total": plain_number(appliances.get("totalDailyUsage")),
            "units": label("p3.units"),
            "cards": [
                (label("p3.card.gen"), f"{plain_number(energy.get('dailyGenerationLow'))}–{plain_number(energy.get('dailyGenerationHigh'))}" if energy else "", label("p3.card.gen.note")),
                (label("p3.card.use"), plain_number(appliances.get("totalDailyUsage")), label("p3.card.use.note")),
                (
                    label("p3.card.surplus"),
                    f"{plain_number(appliances.get('surplusExportedLow'))}–{plain_number(appliances.get('surplusExportedHigh'))}" if appliances.get("surplusExportedLow") is not None else "",
                    label("p3.card.surplus.note"),
                ),
            ],
            "life_now_title": label("p3.lifeNow"),
            "life_solar_title": label("p3.lifeSolar"),
            "life_now": [term(item) for item in content.get("lifeNow") or []],
            "life_solar": [term(item) for item in content.get("lifeSolar") or []],
        },
        "options": {"columns": columns, "service_rows": service_rows, "recommended": system.get("packageTier")},
        "testimonials": {
            "available": bool(testimonials.get("entries")),
            "intro": testimonials.get("intro") or "",
            "entries": [
                {
                    "name": entry.get("name"),
                    "place": entry.get("place") or "",
                    "system": f"{plain_number(entry.get('systemKw'))} kW" if entry.get("systemKw") else "",
                    "installed": entry.get("installedOn") or "",
                    "quote": entry.get("quote") or "",
                    "bill_before": rupees(entry.get("billBefore")),
                    "bill_after": rupees(entry.get("billAfter")),
                    "saving": rupees(entry.get("monthlySaving")),
                    "photo": entry.get("photoUri") if str(entry.get("photoUri") or "").startswith("https://") else "",
                }
                for entry in testimonials.get("entries") or []
            ],
        },
        "savings": {
            "available": bool(savings),
            "current_bill": rupees(savings.get("currentMonthlyBill")),
            "with_solar": f"{rupees(savings.get('postSolarBillLow'))}–{indian(savings.get('postSolarBillHigh'))}" if savings else "",
            "monthly": f"{rupees(savings.get('monthlySavingsLow'))}–{indian(savings.get('monthlySavingsHigh'))}" if savings else "",
            "payback_months": plain_number(savings.get("returnPeriodMonths")),
            "lifetime": rupees(savings.get("lifetimeSavingsAmount")),
            "graph": savings.get("graphPoints") or [],
            "docs_title": label("p8.docs.title"),
            "docs": [pick(item, language) for item in content.get("requiredDocuments") or []],
            "structure_label": pick(((content.get("extraStructureCost") or {}).get("label")), language),
            "structure_value": ((content.get("extraStructureCost") or {}).get("value")) or "",
        },
        "finance": {
            "available": bool(finance),
            "emi": rupees(finance.get("monthlyEMI")),
            "rate": plain_number(finance.get("interestRatePct")),
            "years": plain_number(finance.get("tenureYears")),
            "down_payment": rupees(finance.get("downPaymentAmount")),
            "loan": rupees(finance.get("loanAmount")),
            "post_subsidy": rupees(finance.get("postSubsidyInvestment")),
        },
        "bank": {
            "available": bank_ready,
            "name": company.get("bankName") or "",
            "account_name": company.get("bankAccountName") or "",
            "account_number": company.get("bankAccountNumber") or "",
            "ifsc": company.get("bankIfsc") or "",
            "branch": company.get("bankBranch") or "",
            "upi": company.get("upiId") or "",
            "upi_qr": company.get("upiQrImageUri") or "",
        },
        "journey": {
            "title": label("p9.title"),
            "days": label("p9.days"),
            "steps": [{"days": pick(step.get("days"), language), "title": pick(step.get("title"), language), "body": pick(step.get("body"), language)} for step in content.get("timeline") or []],
            "refund_title": label("p9.refund.title"),
            "refund_body": label("p9.refund.body"),
            "refund_line": label("p9.refund.line", kw=size, amount=derived.get("ksebRefundAmountText") or "") if derived.get("ksebRefundAmount") is not None else "",
            "we_handle_title": label("p9.weHandle"),
            "we_handle": [pick(item, language) for item in content.get("weHandle") or []],
            "customer_scope_title": label("p9.customerScope"),
            "customer_scope": [pick(item, language) for item in content.get("customerScope") or []],
        },
        "terms": {"title": label("p10.title"), "page10": [numbered(i, item) for i, item in enumerate(terms[:split])], "page11": [numbered(split + i, item) for i, item in enumerate(terms[split:])]},
        "summary": {
            "size": f"{size} kW" if size else "",
            "package": selected["name"],
            "system_cost": selected["payable"] or selected["total"],
            "subsidy": selected["subsidy"],
            "investment": selected["after_subsidy"],
            "monthly_savings": rupees(savings.get("monthlySavings")),
            "payback_months": plain_number(savings.get("returnPeriodMonths")),
            "lifetime": rupees(savings.get("lifetimeSavingsAmount")),
            "emi": selected["emi"],
            "emi_rate": selected["emi_rate"],
            "emi_years": selected["emi_years"],
        },
    }
