"""Old URLs the website repo references but no page reaches in a browser (reference lists, pincodes, tariffs,
batteries, metadata, installation stats, the first basic calculator) — through each side's nginx, JSON compared.
Unordered legacy lists (no ORDER BY) are compared as sets keyed by id (legacy-shim parity normalisation rows_by_id)."""

import json
import os
import urllib.error
import urllib.request

SIDES = {"legacy": "http://127.0.0.1:18320", "platform": "http://127.0.0.1:18310"}
UNORDERED = {"/api/device-types/", "/api/wattages/", "/api/room-sizes/", "/api/ev-cars/", "/api/ev-scooters/", "/api/tariffs/", "/api/batteries/", "/api/metadata/"}
REQUESTS = [
    ("GET", "/api/tariffs/", None),
    ("GET", "/api/wattages/", None),
    ("GET", "/api/batteries/", None),
    ("GET", "/api/metadata/", None),
    ("GET", "/api/pincodes/", None),
    ("GET", "/api/installation-stats/?pincode=688503", None),
    ("GET", "/api/installation-stats/?pincode=682001", None),
    ("GET", "/api/installation-stats/?pincode=999999", None),
    ("GET", "/api/installation-stats/", None),
    ("GET", "/api/solar-panels/?minEfficiency=21&sort=efficiency&order=asc", None),
    ("GET", "/api/solar-inverters/?type=hybrid", None),
    ("GET", "/api/emi-calculator/config/", None),
    ("POST", "/api/calculate-solar/", {"pincode": "688503", "property_type": "residential", "monthly_bill": 3000}),
    ("POST", "/api/calculate-solar/", {"pincode": "688503", "property_type": "residential", "monthly_bill": 3000, "ownership_type": "owned"}),
    ("POST", "/api/calculate-solar-new/", {"pincode": "12", "property_type": "residential", "monthly_bill": 3000}),
    ("POST", "/api/emi-calculator/", {"size_id": 999999, "down_payment": 0, "tenure_years": 5}),
    ("POST", "/api/tariffs/", {"name": "x"}),
    ("GET", "/api/tariffs", None),
    ("GET", "/studio-api/api/articles?filters[slug][$eq]=net-metering-explained&populate=*", None),
    ("GET", "/studio-api/api/articles?populate=*&pagination[pageSize]=100", None),
    ("GET", "/studio-api/api/page-content?route=/career", None),
    ("GET", "/studio-api/api/faqs?page=/faq", None),
    ("GET", "/bom/api/quotation-testimonials/", None),
]


def call(base, method, path, body):
    data = json.dumps(body).encode() if body is not None else None
    req = urllib.request.Request(base + path, data=data, method=method, headers={"Content-Type": "application/json"})

    class NoRedirect(urllib.request.HTTPRedirectHandler):
        def redirect_request(self, *a, **k):
            return None

    opener = urllib.request.build_opener(NoRedirect)
    try:
        with opener.open(req, timeout=60) as r:
            return r.status, r.read().decode(), r.headers.get("Location")
    except urllib.error.HTTPError as e:
        return e.code, e.read().decode(), e.headers.get("Location")


def norm(path, text):
    try:
        body = json.loads(text)
    except ValueError:
        return text[:200]
    if path.split("?")[0] in UNORDERED:
        rows = body.get("data", body) if isinstance(body, dict) else body
        if isinstance(rows, list):
            rows = sorted(rows, key=lambda r: r.get("id", 0) if isinstance(r, dict) else 0)
            body = {**body, "data": rows} if isinstance(body, dict) else rows
    return body


if __name__ != "__main__":
    REQUESTS = []
out = []
for method, path, body in REQUESTS:
    res = {side: call(base, method, path, body) for side, base in SIDES.items()}
    same_status = res["legacy"][0] == res["platform"][0]
    same_body = norm(path, res["legacy"][1]) == norm(path, res["platform"][1])
    same_loc = res["legacy"][2] == res["platform"][2]
    tag = "same" if same_status and same_body and same_loc else "DIFF"
    print(f"{tag} {method} {path} -> {res['legacy'][0]} / {res['platform'][0]}" + ("" if same_loc else f" loc {res['legacy'][2]} / {res['platform'][2]}"))
    if not same_body:
        print("     legacy  :", res["legacy"][1][:300])
        print("     platform:", res["platform"][1][:300])
    out.append({"method": method, "path": path, "legacy": res["legacy"], "platform": res["platform"], "same": tag == "same"})
if REQUESTS:
    json.dump(out, open(os.path.join(os.environ.get("UAT_DIR", "."), "results/apisweep.json"), "w"), indent=1)
