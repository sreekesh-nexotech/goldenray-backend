"""Document parity with the Purchase Agreement page: for every record the page saved (``fixtures/pa/flarize_agr.json``),
the page's own ``buildDoc(type, data, lang)`` output (``golden/builddoc.json``, captured by
``fixtures/export_pa_records.mjs``) and the platform template rendered from the imported agreement print the same
text in English, Malayalam and Hindi.

Compared: the document body (title, rows, paragraphs, KSEB and offer boxes, extras, NOTE clause, signature block).
Normalised: white space and digit grouping (the page printed typed amounts raw, ``₹ 335000/–``; the platform groups
them the Indian way, ``₹ 3,35,000/–``), and the phone (the page printed it as typed, ``+91 90000 00102``; the platform
prints the customer's number, ``9000000102``). Not compared, by design: the letterhead (company master instead of hard-coded
strings, D-9) and the platform's additions (``.platform-extra``: agreement number/date, payment details, priced lines).
"""

from __future__ import annotations

import json
import re
from html.parser import HTMLParser
from pathlib import Path

import pytest
from django.template.loader import get_template

from agreements.services.legacy_import import import_pa_agreements
from agreements.tests.factories import kseb_fees

HERE = Path(__file__).parent
RECORDS = json.loads((HERE / "fixtures" / "pa" / "flarize_agr.json").read_text())
GOLDEN = json.loads((HERE / "golden" / "builddoc.json").read_text())["documents"]
VOID = {"br", "img", "input", "meta", "link", "hr", "path", "circle", "rect"}
BLOCK = {"div", "table", "tr", "td", "th", "main", "p"}


class _Text(HTMLParser):
    """Text inside the first element accepted by ``start``; subtrees accepted by ``skip`` are left out."""

    def __init__(self, start, skip):
        super().__init__(convert_charrefs=True)
        self.start, self.skip = start, skip
        self.depth = 0  # >0 while inside the captured element
        self.skipping = 0
        self.done = False
        self.parts: list[str] = []

    def handle_starttag(self, tag, attrs):
        attrs = dict(attrs)
        if tag in VOID:
            if tag == "br" and self.depth and not self.skipping:
                self.parts.append(" ")
            return
        if self.done:
            return
        if self.depth == 0:
            if self.start(tag, attrs):
                self.depth = 1
            return
        if tag in BLOCK and not self.skipping:
            self.parts.append(" ")
        self.depth += 1
        if self.skipping:
            self.skipping += 1
        elif self.skip(tag, attrs):
            self.skipping = 1

    def handle_endtag(self, tag):
        if tag in VOID or self.depth == 0 or self.done:
            return
        if tag in BLOCK and not self.skipping:
            self.parts.append(" ")
        self.depth -= 1
        if self.skipping:
            self.skipping -= 1
        if self.depth == 0:
            self.done = True

    def handle_data(self, data):
        if self.depth and not self.skipping and not self.done:
            self.parts.append(data)


def _normalise(text: str) -> str:
    text = text.replace("\xa0", " ")
    text = re.sub(r"(?<=\d),(?=\d)", "", text)
    return re.sub(r"\s+", " ", text).strip()


def legacy_text(html: str) -> str:
    parser = _Text(lambda tag, attrs: tag == "div" and "padding:130px" in (attrs.get("style") or ""), lambda tag, attrs: False)
    parser.feed(html)
    return _normalise("".join(parser.parts))


def platform_text(html: str) -> str:
    parser = _Text(lambda tag, attrs: tag == "main", lambda tag, attrs: "platform-extra" in (attrs.get("class") or ""))
    parser.feed(html)
    return _normalise("".join(parser.parts))


def _phone_as_printed(record_id: str, text: str) -> str:
    """The page printed the phone as typed; the platform prints the customer's number (national digits)."""
    from customers.services.phones import national_digits, try_normalise

    raw = next(record for record in RECORDS if record["id"] == record_id)["data"].get("phone") or ""
    e164 = try_normalise(raw) if raw else None
    return text.replace(_normalise(raw), national_digits(e164)) if e164 else text


@pytest.fixture
def imported(company):
    kseb_fees()
    import_pa_agreements(RECORDS, profile="crs")
    from agreements.models import Agreement

    return {agreement.legacy_ref.split("/", 1)[1]: agreement for agreement in Agreement.objects.all()}


@pytest.mark.parametrize("language", ["en", "ml", "hi"])
def test_body_text_matches_build_doc(imported, language):
    template = get_template(f"documents/agreement/{language}.html")
    mismatches = {}
    for record_id, documents in GOLDEN.items():
        agreement = imported[record_id]
        expected = _phone_as_printed(record_id, legacy_text(documents[language]))
        actual = platform_text(template.render({"payload": agreement.payload, "language": language}))
        if actual != expected:
            mismatches[record_id] = {"legacy": expected, "platform": actual}
    assert not mismatches, json.dumps(mismatches, ensure_ascii=False, indent=1)


def test_every_saved_record_has_a_golden_and_the_golden_is_not_empty():
    saved = [record["id"] for record in RECORDS if record["id"] not in {"a1", "a2", "a3", "a4", "a5"}]
    assert sorted(saved) == sorted(GOLDEN) and len(saved) == 7
    for documents in GOLDEN.values():
        for html in documents.values():
            assert len(legacy_text(html)) > 200


def test_platform_additions_carry_the_company_master(imported):
    agreement = next(agreement for agreement in imported.values() if agreement.kind == "SALE_ORDER")
    html = get_template("documents/agreement/en.html").render({"payload": agreement.payload, "language": "en"})
    assert "M/s Golden Ray Renewable Energy LLP" in html and "SBIN0001234" in html and agreement.number in html
    assert "Golden Ray Renewable Energy" in html and "https://www.goldenray.co.in" in html
