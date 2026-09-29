"""The parity harness (tools/parity/): comparison rules, normalisers, approvals and the committed corpus."""

import importlib.util
import json
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[2]
SPEC = importlib.util.spec_from_file_location("parity_harness", ROOT / "tools" / "parity" / "harness.py")
harness = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(harness)


def _result(status=200, body=None, ctype="application/json", location=None):
    raw = b"" if body is None else (body if isinstance(body, bytes) else json.dumps(body).encode())
    return {"status": status, "raw": raw, "ctype": ctype, "location": location}


def test_key_order_and_number_types_are_compared():
    case = {"id": "x", "normalise": []}
    assert harness.compare(case, _result(body={"a": 1, "b": 2}), _result(body={"a": 1, "b": 2})) is None
    assert harness.compare(case, _result(body={"a": 1, "b": 2}), _result(body={"b": 2, "a": 1}))
    assert harness.compare(case, _result(body={"a": 1}), _result(body={"a": 1.0}))
    assert harness.compare(case, _result(status=400), _result(status=404)) == "status 400 ≠ 404"


def test_redirects_compare_the_location_path():
    case = {"id": "x"}
    assert harness.compare(case, _result(301, location="http://127.0.0.1:18012/api/x/?a=1"), _result(301, location="/api/x/?a=1")) is None
    assert harness.compare(case, _result(301, location="/api/x/"), _result(301, location="/legacy/api/x/"))


def test_html_error_pages_compare_by_status():
    case = {"id": "x"}
    assert harness.compare(case, _result(404, b"<html>", "text/html"), _result(404, {"code": "not_found"})) is None


def test_normalisers():
    rows = [{"id": 3, "s": 1}, {"id": 1, "s": 1}, {"id": 2, "s": 0}]
    assert [row["id"] for row in harness.rows_by_id(list(rows))] == [1, 2, 3]
    assert [row["id"] for row in harness.normaliser("ties_by_id:s")({"data": list(rows)})["data"]] == [1, 3, 2]
    assert harness.normaliser("created_row")({"data": {"id": 9, "name": "a"}}) == {"data": {"id": "<volatile>", "name": "a"}}


def test_approvals_are_narrow():
    case = {"id": "bom/calculate/001"}
    legacy = _result(body={"bom_lines": [1], "cost_breakdown": {}, "totals": {}, "pricing": {"p": 1}})
    good = _result(body={"bom_lines": [1], "pricing": {"p": 1}})
    wrong = _result(body={"bom_lines": [2], "pricing": {"p": 1}})
    entry = {"prefix": "bom/calculate/", "statuses": [200, 200], "drop_keys": ["cost_breakdown", "totals"], "reason": "B-1"}
    assert harness.approval(case, legacy, good, [entry]) is entry
    assert harness.approval(case, legacy, wrong, [entry]) is None
    masked = {"id": "bom/calculate/001", "statuses": [400, 400], "mask": ["error"], "reason": "text"}
    assert harness.approval(case, _result(400, {"error": "a"}), _result(400, {"error": "b"}), [masked]) is masked
    assert harness.approval(case, _result(400, {"error": "a", "x": 1}), _result(400, {"error": "b"}), [masked]) is None
    assert harness.approval(case, _result(500), _result(400), [masked]) is None


def test_write_path_requests_never_go_to_the_shared_servers(tmp_path):
    corpus = tmp_path / "c.jsonl"
    corpus.write_text("")
    with pytest.raises(SystemExit):
        harness.main(["--corpus", str(corpus), "--legacy-backend-writes", "http://127.0.0.1:18012", "--new", "http://127.0.0.1:1"])


def test_committed_corpus_covers_every_shimmed_url():
    cases = [json.loads(line) for line in (ROOT / "tools/parity/corpus.jsonl").read_text().splitlines()]
    assert len(cases) >= 400 and len({case["id"] for case in cases}) == len(cases)
    paths = {case["path"].rstrip("/") for case in cases}
    from legacy import urls

    for pattern in urls.legacy_urlpatterns:
        route = "/" + str(pattern.pattern).replace("<slug:api_uid>", "articles").replace("<slug:slug>", "solar-installation-engineer")
        assert route.rstrip("/") in paths, route
    assert all(case["write"] for case in cases if case["group"] == "forms")
    groups = {case["group"] for case in cases}
    assert {"reference", "products", "calculators", "emi", "bom", "forms", "cms", "seo", "installations"} <= groups
    assert sum(1 for case in cases if case["path"] == "/bom/api/calculate/") >= 70


def test_approved_file_is_valid():
    entries = json.loads((ROOT / "tools/parity/approved.json").read_text())
    assert entries and all(entry.get("reason") and (entry.get("id") or entry.get("prefix") or entry.get("pattern")) for entry in entries)
