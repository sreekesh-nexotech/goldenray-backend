"""Formatting for the agreement document templates — the texts of the Purchase Agreement page's ``buildDoc`` in
English, Malayalam and Hindi, and Indian number grouping. No business logic: the payload already holds every value."""

from __future__ import annotations

from decimal import Decimal, InvalidOperation

from django import template

register = template.Library()

LABELS = {
    "en": {
        "title": "PURCHASE AGREEMENT",
        "quotation_no": "QUOTATION NO",
        "name": "NAME",
        "address": "ADDRESS",
        "phone": "PH NO",
        "size": "SOLAR SIZE",
        "phase": "PHASE",
        "panel": "PANEL BRAND",
        "panel_capacity": "SOLAR PANEL CAPACITY",
        "inverter_brand": "INVERTER BRAND",
        "inverter_type": "INVERTER TYPE",
        "battery": "BATTERY OPTION",
        "structure": "STRUCTURE MATERIAL",
        "extra_structure": "EXTRA STRUCTURE",
        "walkway": "WALK WAY",
        "ladder": "LADDER",
        "orig": "ORIGINAL PROJECT PRICE",
        "extra": "EXTRA STRUCTURE COST",
        "disc": "DISCOUNT / OFFER AMOUNT",
        "fin": "FINAL PROJECT PRICE PAYABLE",
        "tot": "TOTAL COST",
        "kseb": "KSEB Registration Fee",
        "kseb_note": "Fee as per the required capacity",
        "offer": "Additional Offer",
        "extras_head": "Additional Structure & Charges",
        "extras_extra": "Extra Structure",
        "note": (
            "In the event of a shortage or unavailability of the mentioned solar panels or inverter models, the company reserves the "
            "right to replace them with equivalent or higher-quality products from another reputed brand, ensuring the same performance standards and warranty coverage."
        ),
        "date": "Date",
        "place": "Place",
        "signature": "(Signature)",
        "agreement_no": "Agreement No",
        "issued_on": "Date",
        "payment": "Payment details",
        "bank": "Bank",
        "account_name": "Account name",
        "account_number": "Account number",
        "ifsc": "IFSC",
        "upi": "UPI",
        "lines_head": "Additional work",
        "line_description": "Description",
        "line_quantity": "Qty",
        "line_rate": "Rate",
        "line_amount": "Amount",
    },
    "ml": {
        "title": "പർച്ചേസ് എഗ്രിമെൻ്റ്",
        "quotation_no": "ക്വട്ടേഷൻ നം",
        "name": "പേര്",
        "address": "വിലാസം",
        "phone": "ഫോൺ നമ്പർ",
        "size": "സോളാർ സൈസ്",
        "phase": "ഫേസ്",
        "panel": "പാനൽ ബ്രാൻഡ്",
        "panel_capacity": "സോളാർ പാനൽ കപ്പാസിറ്റി",
        "inverter_brand": "ഇൻവെർട്ടർ ബ്രാൻഡ്",
        "inverter_type": "ഇൻവെർട്ടർ ടൈപ്പ്",
        "battery": "ബാറ്ററി ഓപ്ഷൻ",
        "structure": "ഘടന മെറ്റീരിയൽ",
        "extra_structure": "അധിക ഘടന",
        "walkway": "വാക്ക് വേ",
        "ladder": "ലാഡർ",
        "orig": "അസൽ പ്രോജക്ട് വില",
        "extra": "അധിക ഘടന ചിലവ്",
        "disc": "ഡിസ്കൗണ്ട് / ഓഫർ തുക",
        "fin": "അന്തിമ പ്രോജക്ട് വില",
        "tot": "മൊത്തം ചിലവ്",
        "kseb": "കെഎസ്ഇബി രജിസ്ട്രേഷൻ ഫീസ്",
        "kseb_note": "ആവശ്യമായ കപ്പാസിറ്റി അനുസരിച്ചുള്ള ഫീസ്",
        "offer": "അധിക ഓഫർ",
        "extras_head": "അധിക ഘടനയും ചാർജുകളും",
        "extras_extra": "അധിക ഘടന",
        "note": (
            "പറഞ്ഞിരിക്കുന്ന സോളാർ പാനലുകളുടെയോ ഇൻവെർട്ടർ മോഡലുകളുടെയോ ക്ഷാമമോ ലഭ്യതക്കുറവോ ഉണ്ടായാൽ, അതേ പ്രകടന നിലവാരവും വാറണ്ടി പരിരക്ഷയും ഉറപ്പാക്കിക്കൊണ്ട് "
            "മറ്റൊരു പ്രശസ്ത ബ്രാൻഡിലെ തത്തുല്യമോ ഉയർന്ന ഗുണനിലവാരമുള്ളതോ ആയ ഉൽപ്പന്നങ്ങൾ ഉപയോഗിച്ച് അവ മാറ്റിസ്ഥാപിക്കാനുള്ള അവകാശം കമ്പനിക്ക് ഉണ്ടായിരിക്കും."
        ),
        "date": "തീയതി",
        "place": "സ്ഥലം",
        "signature": "(ഒപ്പ്)",
        "agreement_no": "എഗ്രിമെൻ്റ് നം",
        "issued_on": "തീയതി",
        "payment": "പേയ്മെൻ്റ് വിവരങ്ങൾ",
        "bank": "ബാങ്ക്",
        "account_name": "അക്കൗണ്ട് പേര്",
        "account_number": "അക്കൗണ്ട് നമ്പർ",
        "ifsc": "IFSC",
        "upi": "UPI",
        "lines_head": "അധിക ജോലികൾ",
        "line_description": "വിവരണം",
        "line_quantity": "എണ്ണം",
        "line_rate": "നിരക്ക്",
        "line_amount": "തുക",
    },
    "hi": {
        "title": "परचेज़ एग्रीमेंट",
        "quotation_no": "कोटेशन नं",
        "name": "नाम",
        "address": "पता",
        "phone": "फोन नंबर",
        "size": "सोलर साइज़",
        "phase": "फेज़",
        "panel": "पैनल ब्रांड",
        "panel_capacity": "सोलर पैनल क्षमता",
        "inverter_brand": "इन्वर्टर ब्रांड",
        "inverter_type": "इन्वर्टर प्रकार",
        "battery": "बैटरी विकल्प",
        "structure": "स्ट्रक्चर मटीरियल",
        "extra_structure": "एक्स्ट्रा स्ट्रक्चर",
        "walkway": "वॉक वे",
        "ladder": "लैडर",
        "orig": "मूल प्रोजेक्ट मूल्य",
        "extra": "अतिरिक्त संरचना लागत",
        "disc": "छूट / ऑफर राशि",
        "fin": "अंतिम देय प्रोजेक्ट मूल्य",
        "tot": "कुल लागत",
        "kseb": "केएसईबी पंजीकरण शुल्क",
        "kseb_note": "आवश्यक क्षमता के अनुसार शुल्क",
        "offer": "अतिरिक्त ऑफर",
        "extras_head": "अतिरिक्त संरचना और शुल्क",
        "extras_extra": "अतिरिक्त संरचना",
        "note": (
            "उल्लिखित सोलर पैनल या इन्वर्टर मॉडल की कमी या अनुपलब्धता की स्थिति में, कंपनी को समान प्रदर्शन मानकों और वारंटी कवरेज "
            "को सुनिश्चित करते हुए किसी अन्य प्रतिष्ठित ब्रांड के समकक्ष या उच्च गुणवत्ता वाले उत्पादों से उन्हें बदलने का अधिकार सुरक्षित है।"
        ),
        "date": "दिनांक",
        "place": "स्थान",
        "signature": "(हस्ताक्षर)",
        "agreement_no": "एग्रीमेंट नं",
        "issued_on": "दिनांक",
        "payment": "भुगतान विवरण",
        "bank": "बैंक",
        "account_name": "खाता नाम",
        "account_number": "खाता संख्या",
        "ifsc": "IFSC",
        "upi": "UPI",
        "lines_head": "अतिरिक्त कार्य",
        "line_description": "विवरण",
        "line_quantity": "मात्रा",
        "line_rate": "दर",
        "line_amount": "राशि",
    },
}
INVERTER_LABELS = {
    "en": {"STRING": "String Inverter", "MICRO": "Micro Inverter", "HYBRID": "Hybrid Inverter", "": "String Inverter"},
    "ml": {"STRING": "string ഇൻവെർട്ടർ", "MICRO": "micro ഇൻവെർട്ടർ", "HYBRID": "ഹൈബ്രിഡ് ഇൻവെർട്ടർ", "": "string ഇൻവെർട്ടർ"},
    "hi": {"STRING": "स्ट्रिंग इन्वर्टर", "MICRO": "माइक्रो इन्वर्टर", "HYBRID": "हाइब्रिड इन्वर्टर", "": "स्ट्रिंग इन्वर्टर"},
}


@register.simple_tag
def agreement_labels(language):
    return LABELS.get(language, LABELS["en"])


@register.simple_tag
def inverter_label(inverter_type, language):
    return INVERTER_LABELS.get(language, INVERTER_LABELS["en"]).get(inverter_type or "", INVERTER_LABELS["en"]["STRING"])


def _group_indian(digits: str) -> str:
    if len(digits) <= 3:
        return digits
    head, tail = digits[:-3], digits[-3:]
    groups = []
    while len(head) > 2:
        groups.insert(0, head[-2:])
        head = head[:-2]
    if head:
        groups.insert(0, head)
    return ",".join(groups) + "," + tail


@register.filter
def inr(value):
    """``235000.00`` → ``2,35,000``; ``1234.50`` → ``1,234.50`` (``toLocaleString('en-IN')`` grouping)."""
    if value in (None, ""):
        return ""
    try:
        number = Decimal(str(value))
    except (InvalidOperation, ValueError):
        return value
    sign = "-" if number < 0 else ""
    number = abs(number)
    whole, _, fraction = f"{number:.2f}".partition(".")
    grouped = _group_indian(whole)
    return f"{sign}{grouped}" if fraction == "00" else f"{sign}{grouped}.{fraction}"


@register.filter
def quantity(value):
    """``3.00`` → ``3``; ``2.50`` → ``2.5``."""
    if value in (None, ""):
        return ""
    try:
        return f"{Decimal(str(value)).normalize():f}"
    except (InvalidOperation, ValueError):
        return value
