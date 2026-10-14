"""The BOM as a CSV: the quotation sheet, or the warehouse's picking list.

Two shapes, matching the two the source tool downloads (``App.jsx:2545-2567``):

* ``quotation`` — number, material, qty, unit, unit price, amount, GST %.
* ``dispatch``  — number, material, qty, unit. **No prices at all.**

Built from a quote body rather than from the models, so the sheet can never disagree with
the screen it was downloaded from: both are the same ``website_quote.quote()`` result.

One fix to the source on the way through. It prints a literal ``9%`` in the GST column of
every line::

    if (isQ) r += `,${item.unitPrice},${item.amount},9%`;

Which is right only while the composite regime is on and its rate is exactly 9. Under the
per-line regime the lines genuinely carry 5, 12 and 18 %, and a sheet that labels an 18 %
item as 9 % is wrong in a document someone may file for tax. So the rate comes from the
line; for the composite case, where the line buckets are deliberately zeroed, it is
recovered from the totals (see :func:`_composite_rate_pct`) rather than read from a key —
the response does not carry the rate, and adding one for this module's benefit would have
been the wrong fix.
"""

from __future__ import annotations

import csv
import io

QUOTATION_HEADER = ("No", "Material / Brand", "Qty", "Unit", "Unit Price", "Amount", "GST %")
DISPATCH_HEADER = ("No", "Material / Brand", "Qty", "Unit")


def _gst_label(line: dict, composite_rate_pct: float | None) -> str:
    """The rate this line is actually taxed at.

    Under COMPOSITE the whole bill is taxed once and the per-line buckets are zeroed, so
    the line's own ``gst_pct`` is 0 and the quote's effective rate is the truthful figure.
    Under PER_LINE the line's rate is the real one and differs between lines.
    """
    pct = line.get("gst_pct")
    if not pct and composite_rate_pct:
        pct = composite_rate_pct
    if not pct:
        return ""
    return f"{pct:g}%"


def _composite_rate_pct(body: dict) -> float | None:
    """The single rate the whole bill was taxed at, or None if it was taxed per line.

    Derived from the totals rather than read from a key, because the response does not
    carry the rate: under COMPOSITE the per-line buckets are zeroed and ``gst_total`` is
    the one charge, so ``gst_total / material_ex_gst`` recovers it. Returns None whenever
    the buckets are populated (PER_LINE) or the caller may not see totals at all, and in
    both of those cases the line's own rate is the right answer anyway.
    """
    totals = body.get("totals") or {}
    material = totals.get("material_ex_gst") or 0
    gst_total = totals.get("gst_total") or 0
    per_line = (totals.get("gst_5") or 0) + (totals.get("gst_12") or 0) + (totals.get("gst_18") or 0)
    if not material or not gst_total or per_line:
        return None
    return round(gst_total * 100 / material, 2)


def quote_csv(body: dict, *, mode: str = "quotation") -> str:
    """``body`` is a ``website_quote.quote()`` result (or its public/dispatch filtering)."""
    dispatch = mode == "dispatch"
    buffer = io.StringIO()
    writer = csv.writer(buffer, lineterminator="\n")
    writer.writerow(DISPATCH_HEADER if dispatch else QUOTATION_HEADER)

    composite_rate = _composite_rate_pct(body)

    for index, line in enumerate(body.get("bom_lines") or [], start=1):
        row = [index, line.get("name", ""), line.get("qty", ""), line.get("unit") or "nos"]
        if not dispatch:
            row += [line.get("unit_price", ""), line.get("amount", ""), _gst_label(line, composite_rate)]
        writer.writerow(row)

    return buffer.getvalue()


def filename(body: dict, *, mode: str = "quotation") -> str:
    """``GoldenRay_BOM_<label>_<mode>.csv``, as the source names its download.

    The label identifies the configuration so two sheets in a downloads folder can be
    told apart -- on the upgrade path that is the kW transition, on hybrid the battery
    band, and otherwise the tier.
    """
    meta = body.get("meta") or {}
    system_type = str(meta.get("system_type") or "").lower()
    if system_type == "upgrade":
        # `size` is the transition itself ("3→5"), so the arrow becomes "to".
        label = f"Upgrade_{_plain(str(meta.get('size') or '').replace('→', 'to'))}kW"
    elif system_type == "hybrid":
        label = f"Hybrid_{_plain(meta.get('bat_config'))}Bat"
    else:
        label = _plain(meta.get("tier")) or "bom"
    return f"GoldenRay_BOM_{label}_{mode}.csv"


def _plain(value) -> str:
    """A number for a filename: 5 rather than 5.0, and nothing unsafe for a filesystem."""
    if value is None:
        return ""
    if isinstance(value, float) and value.is_integer():
        value = int(value)
    return "".join(ch for ch in str(value) if ch.isalnum() or ch in "-_")
