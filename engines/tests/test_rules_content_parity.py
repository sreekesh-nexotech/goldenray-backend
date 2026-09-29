"""Golden parity: the bilingual fit guard (en/ml), content helpers, content-store lifecycle, branding freeze and store."""

from __future__ import annotations

import pytest

from engines import content_fit
from engines.tests.rules_golden import assert_error, assert_same, cases, frozen, ids, load

FILE = "rules_content.json"
GOLDEN = load(FILE)


def test_limits_and_constants():
    header = GOLDEN["header"]
    assert_same(header["limits"], content_fit.LIMITS)
    assert header["storeVersion"] == content_fit.CONTENT_STORE_VERSION
    assert header["brandingKinds"] == list(content_fit.BRANDING_KINDS)


@pytest.mark.parametrize("case", cases(FILE, "fit"), ids=ids(cases(FILE, "fit")))
def test_fit_guard(case):
    content, rate = frozen(case["input"]["content"]), case["input"]["dailyGenPerKw"]
    assert_same(case["output"], content_fit.validate_content(content, rate))
    if "summary" in case:
        assert_same(case["summary"], content_fit.fit_summary(content, rate))


def test_fit_cases_cover_both_languages_at_the_boundary():
    by_id = {case["id"]: case for case in cases(FILE, "fit")}
    assert by_id["published content fits"]["output"] == []
    assert by_id["label en exactly at the limit"]["output"] == []
    assert [error["path"].rsplit(".", 1)[1] for error in by_id["label ml at 360, one over"]["output"]] == ["ml"]
    assert by_id["label counted in UTF-16 units (emoji)"]["output"][0]["length"] == 301


@pytest.mark.parametrize("case", cases(FILE, "helpers"), ids=ids(cases(FILE, "helpers")))
def test_helpers(case):
    data, fn = case["input"], case["fn"]
    published = frozen(GOLDEN["refs"]["published"])
    if fn == "pick":
        result = content_fit.pick(frozen(data["field"]), data["lang"])
    elif fn == "normaliseLanguage":
        result = content_fit.normalise_language(data["value"])
    elif fn == "label":
        source = frozen(data["content"]) if "content" in data else published
        result = content_fit.label(source, data["key"], data["lang"], frozen(data["vars"]))
    elif fn == "profileKeyForKw":
        result = content_fit.profile_key_for_kw(frozen(data["profiles"]), data["kw"])
    elif fn == "defaultRowsForKw":
        result = content_fit.default_rows_for_kw(published, data["kw"])
    elif fn == "dailyGenerationForKw":
        result = content_fit.daily_generation_for_kw(data["kw"], data["rate"])
    elif fn == "materialiseRows":
        result = content_fit.materialise_rows(published, frozen(data["rows"]), data["lang"])
    else:
        result = content_fit.total_units(frozen(data["rows"]))
    assert_same(case["output"], result)


@pytest.mark.parametrize("case", cases(FILE, "store"), ids=ids(cases(FILE, "store")))
def test_content_store(case):
    data, fn = case["input"], case["fn"]
    store = frozen(data.get("store"))
    if fn == "createEmptyStore":
        call = lambda: content_fit.create_empty_store(frozen(data["content"]))  # noqa: E731
    elif fn == "saveDraft":
        call = lambda: content_fit.save_draft(store, frozen(data["content"]), actor_id=data["actorId"], at=data["at"])  # noqa: E731
    elif fn == "publishDraft":
        call = lambda: content_fit.publish_draft(store, actor_id=data["actorId"], at=data["at"])  # noqa: E731
    elif fn == "discardDraft":
        call = lambda: content_fit.discard_draft(store)  # noqa: E731
    elif fn == "describeStore":
        call = lambda: content_fit.describe_store(store)  # noqa: E731
    else:
        call = lambda: {"published": None if content_fit.get_published(store) is None else "content", "draft": content_fit.get_draft({})}  # noqa: E731
    if "error" in case:
        with pytest.raises(content_fit.ContentInvalid) as caught:
            call()
        assert_error(case["error"], caught.value)
        assert caught.value.errors
    else:
        assert_same(case["output"], call())


_BRANDING_WRITE = {"accountId": "account_id", "label": "label", "isDemo": "is_demo", "versionId": "version_id", "makePrimary": "make_primary"}


@pytest.mark.parametrize("case", cases(FILE, "branding"), ids=ids(cases(FILE, "branding")))
def test_branding(case):
    data, fn = case["input"], case["fn"]
    store = frozen(data.get("store", {}))
    if fn == "freezeBrandingSnapshot":
        call = lambda: content_fit.freeze_branding_snapshot(store, frozen(data["overrides"]))  # noqa: E731
    elif fn == "listAccounts":
        call = lambda: content_fit.list_accounts(store, data["kind"])  # noqa: E731
    elif fn == "kindContainsDemo":
        call = lambda: content_fit.kind_contains_demo(store, data["kind"])  # noqa: E731
    elif fn == "describeBrandingStore":
        call = lambda: content_fit.describe_branding_store(store)  # noqa: E731
    elif fn == "currentVersionId":
        call = lambda: content_fit.current_version_id(store, data["kind"])  # noqa: E731
    elif fn == "getVersion":
        call = lambda: content_fit.get_version(store, data["kind"], data["versionId"])  # noqa: E731
    elif fn == "publishBrandingVersion":
        extra = {name: data[key] for key, name in _BRANDING_WRITE.items() if key in data}
        call = lambda: content_fit.publish_branding_version(store, actor_id=data["actorId"], kind=data["kind"], values=frozen(data["values"]), at=data["at"], **extra)  # noqa: E731
    elif fn == "setBrandingAccountStatus":
        call = lambda: content_fit.set_branding_account_status(store, actor_id=data["actorId"], kind=data["kind"], account_id=data["accountId"], status=data["status"], at=data["at"])  # noqa: E731
    else:
        call = lambda: content_fit.set_primary_branding_account(store, actor_id=data["actorId"], kind=data["kind"], account_id=data["accountId"], at=data["at"])  # noqa: E731
    if "error" in case:
        with pytest.raises(content_fit.BrandingError) as caught:
            call()
        assert_error(case["error"], caught.value)
    else:
        assert_same(case["output"], call())
