"""POST /api/v1/bom/quote/ and GET /api/v1/bom/quote-options/ — the internal BOM calculator."""

import csv
import io
import json

import pytest

pytestmark = pytest.mark.django_db
QUOTE = "/api/v1/bom/quote/"
OPTIONS = "/api/v1/bom/quote-options/"
CSV = "/api/v1/bom/quote/csv/"
BODY = {"sys_type": "ongrid", "size": "3", "tier": "base"}


def test_permissions(api_client, outsider, viewer, world):
    assert api_client.post(QUOTE, BODY, format="json").status_code == 401
    assert outsider.post(QUOTE, BODY, format="json").status_code == 403
    assert viewer.post(QUOTE, BODY, format="json").status_code == 200
    assert api_client.get(OPTIONS).status_code == 401
    assert outsider.get(OPTIONS).status_code == 403
    assert viewer.get(OPTIONS).status_code == 200


def test_internal_cost_and_margin_only_with_pricing_internal(auth_client, make_user, world):
    plain = auth_client(make_user(grants={"bom": ["view"]})).post(QUOTE, BODY, format="json")
    assert plain.status_code == 200 and "cost_breakdown" not in plain.json() and "totals" not in plain.json()
    internal = auth_client(make_user(grants={"bom": ["view"], "pricing_internal": ["view"]})).post(QUOTE, BODY, format="json")
    body = internal.json()
    assert internal.status_code == 200 and body["cost_breakdown"]["cost_total"] > 0 and body["totals"]["grand_total"] > 0
    assert body["pricing"] == plain.json()["pricing"] and internal["Cache-Control"] == "no-store"


def test_dispatch_mode_strips_every_price_server_side(auth_client, make_user, world):
    """The picking list must not carry prices even to a caller who may see costs.

    Hiding the columns in the browser would still put the customer's price on a machine
    on the warehouse floor, so the stripping has to happen here. Asserted for the
    *internal* role, because that is the one that would otherwise leak the most.
    """
    client = auth_client(make_user(grants={"bom": ["view"], "pricing_internal": ["view"]}))
    body = client.post(QUOTE, {**BODY, "mode": "dispatch"}, format="json").json()

    assert "pricing" not in body and "available_offers" not in body
    assert "cost_breakdown" not in body and "totals" not in body
    assert body["bom_lines"], "a picking list with no lines is not a picking list"
    for line in body["bom_lines"]:
        assert not {"unit_price", "amount", "gst_pct", "gst_amt"} & set(line), line
        # What the warehouse actually needs has to survive the stripping.
        assert line["name"] and line["qty"] and line["unit"]
    # No price anywhere in the payload, whatever the key is called.
    assert "unit_price" not in json.dumps(body)


def test_quotation_mode_is_unchanged_and_the_default(auth_client, make_user, world):
    client = auth_client(make_user(grants={"bom": ["view"], "pricing_internal": ["view"]}))
    default = client.post(QUOTE, BODY, format="json").json()
    explicit = client.post(QUOTE, {**BODY, "mode": "quotation"}, format="json").json()
    assert default == explicit
    assert default["pricing"]["customer_price"] > 0 and default["totals"]["grand_total"] > 0


def test_the_public_endpoint_still_ignores_mode(api_client, world, settings):
    """Legacy parity: the public contract documents `mode` as accepted and ignored."""
    rates = settings.REST_FRAMEWORK["DEFAULT_THROTTLE_RATES"]
    settings.REST_FRAMEWORK = {**settings.REST_FRAMEWORK, "DEFAULT_THROTTLE_RATES": {**rates, "public_write": "1000/min"}}
    public = "/api/public/v1/bom/quote/"
    plain = api_client.post(public, BODY, format="json").json()
    with_mode = api_client.post(public, {**BODY, "mode": "dispatch"}, format="json").json()
    assert plain == with_mode and "pricing" in with_mode


def test_csv_export_carries_each_line_s_own_gst_rate(auth_client, make_user, world):
    """The source prints a literal `9%` on every line; the lines are 5/12/18 under PER_LINE.

    A sheet that labels an 18% item as 9% is wrong in a document someone may file, so the
    rate comes from the line.
    """
    client = auth_client(make_user(grants={"bom": ["view"], "pricing_internal": ["view"]}))
    response = client.post(CSV, BODY, format="json")
    assert response.status_code == 200
    assert response["Content-Type"].startswith("text/csv")
    assert "attachment; filename=" in response["Content-Disposition"]

    text = response.content.decode()
    header, *rows = [line for line in text.splitlines() if line]
    assert header == "No,Material / Brand,Qty,Unit,Unit Price,Amount,GST %"
    assert rows, "a quotation sheet with no lines"
    rates = {row.rsplit(",", 1)[-1] for row in rows}
    # The fixture's panel is 5% and its ACDB-ish rows are not, so a single rate for every
    # line would mean the per-line rate was being ignored.
    assert rates != {"9%"}, f"every line labelled 9%: {rates}"
    assert all(rate.endswith("%") for rate in rates), rates


def test_csv_dispatch_mode_has_no_prices(auth_client, make_user, world):
    """Asserted for the role that *may* see costs, since that is the one that would leak."""
    client = auth_client(make_user(grants={"bom": ["view"], "pricing_internal": ["view"]}))
    text = client.post(CSV, {**BODY, "mode": "dispatch"}, format="json").content.decode()
    rows = list(csv.reader(io.StringIO(text)))
    header, body = rows[0], [row for row in rows[1:] if row]

    assert header == ["No", "Material / Brand", "Qty", "Unit"]
    assert body, "a picking list with no lines is not a picking list"
    # Parsed as CSV rather than split on commas, so a quoted name containing a comma
    # cannot smuggle a fifth field past the check.
    assert all(len(row) == 4 for row in body), body
    assert "%" not in text, "a picking list must not carry a GST rate"

    # Cross-check against the priced sheet: every figure it carries must be absent here.
    priced = client.post(CSV, BODY, format="json").content.decode()
    prices = {
        cell
        for row in list(csv.reader(io.StringIO(priced)))[1:]
        for cell in row[4:6]
        if cell and cell not in {"1", "0"}
    }
    assert prices, "the priced sheet carries no prices, so this proves nothing"
    assert not prices & {cell for row in body for cell in row}, prices


def test_csv_needs_the_same_permission_as_the_quote(api_client, outsider, viewer, world):
    assert api_client.post(CSV, BODY, format="json").status_code == 401
    assert outsider.post(CSV, BODY, format="json").status_code == 403
    assert viewer.post(CSV, BODY, format="json").status_code == 200


def test_csv_validation_matches_the_json_endpoint_exactly(viewer, world):
    """Whatever the JSON quote accepts or refuses, the CSV of it does the same.

    Worth pinning because the two could easily drift: a bad `dist_km` is a 400 from the
    request serializer, while an unknown `size` is a 200 with an empty BOM -- the legacy
    engine never validated the size against the template, and the golden corpus encodes
    that. The CSV must not be stricter or laxer than the sheet's own source.
    """
    for body in ({**BODY, "dist_km": "12.5"}, {**BODY, "size": "nonsense"}, BODY):
        assert viewer.post(CSV, body, format="json").status_code == viewer.post(QUOTE, body, format="json").status_code, body

    # And the empty case is an empty sheet, not a broken one: the header is still there,
    # so it opens in a spreadsheet and plainly shows nothing was configured.
    text = viewer.post(CSV, {**BODY, "size": "nonsense"}, format="json").content.decode()
    assert text.splitlines()[0] == "No,Material / Brand,Qty,Unit,Unit Price,Amount,GST %"


def test_validation_envelope(viewer, world):
    response = viewer.post(QUOTE, {**BODY, "dist_km": "12.5"}, format="json")
    assert response.status_code == 400 and response.json()["code"] == "validation_error"


def test_quote_options(viewer, world):
    body = viewer.get(OPTIONS).json()
    assert "ongrid" in body["system_types"] and body["structure_types"] == ["flatRoof", "elevated", "sheetRoof"]
    assert set(body["system_types"]["ongrid"]) == {"name", "sizes", "three_phase_sizes", "tiers", "battery_configs"}


# ── Content Studio single sign-in (HS256 token signed by the CMS) ──

STUDIO_KEY = "studio-test-key-0123456789-0123456789"


def _studio_token(email="bom@example.com", **claims):
    import time

    import jwt

    payload = {"token_type": "access", "exp": int(time.time()) + 600, "user_id": 7, "username": "bom", "email": email, **claims}
    return jwt.encode(payload, STUDIO_KEY, algorithm="HS256")


def _studio_client(api_client, token):
    api_client.credentials(HTTP_AUTHORIZATION=f"Bearer {token}")
    return api_client


@pytest.fixture
def studio_user(make_user, settings):
    settings.STUDIO_JWT_SIGNING_KEY = STUDIO_KEY
    return make_user(email="bom@example.com", grants={"bom": ["view"], "pricing_internal": ["view"]})


def test_studio_token_uses_the_platform_role(api_client, studio_user, world):
    response = _studio_client(api_client, _studio_token("BOM@example.com")).post(QUOTE, BODY, format="json")
    assert response.status_code == 200 and "cost_breakdown" in response.json()
    assert _studio_client(api_client, _studio_token("BOM@example.com")).get(OPTIONS).status_code == 200


def test_studio_token_without_bom_grant_is_forbidden(api_client, make_user, settings, world):
    settings.STUDIO_JWT_SIGNING_KEY = STUDIO_KEY
    make_user(email="other@example.com", grants={"catalog": ["view"]})
    assert _studio_client(api_client, _studio_token("other@example.com")).post(QUOTE, BODY, format="json").status_code == 403


@pytest.mark.parametrize(
    "token",
    [
        pytest.param(lambda: _studio_token("nobody@example.com"), id="unknown-email"),
        pytest.param(lambda: _studio_token(email=""), id="no-email"),
        pytest.param(lambda: _studio_token(token_type="refresh"), id="refresh-token"),
        pytest.param(lambda: _studio_token(exp=1), id="expired"),
        pytest.param(lambda: __import__("jwt").encode({"token_type": "access", "email": "bom@example.com"}, "wrong-key-0123456789-0123456789ab", algorithm="HS256"), id="bad-signature"),
    ],
)
def test_bad_studio_tokens_are_401(api_client, studio_user, world, token):
    assert _studio_client(api_client, token()).post(QUOTE, BODY, format="json").status_code == 401


def test_studio_tokens_are_refused_when_not_configured_and_elsewhere(api_client, make_user, settings, world):
    make_user(email="bom@example.com", grants={"bom": ["view"]})
    settings.STUDIO_JWT_SIGNING_KEY = ""
    assert _studio_client(api_client, _studio_token()).post(QUOTE, BODY, format="json").status_code == 401
    settings.STUDIO_JWT_SIGNING_KEY = STUDIO_KEY
    # only the views that opt in accept it: the dry-build view keeps platform tokens only
    assert _studio_client(api_client, _studio_token()).post("/api/v1/bom/build/", {"system_type": "ONGRID", "size": "3", "tier": "BASE"}, format="json").status_code == 401
