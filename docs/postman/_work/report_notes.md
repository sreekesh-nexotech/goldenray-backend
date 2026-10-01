## Independent verification (Phase 5)

Two fresh verifier agents checked the collection. Neither had seen the work, and both were told not to read `docs/postman/_work/`.

**1. Adversarial coverage hunt.** This agent wrote its own resolver walk in the API container and its own static read of every `urls.py`, router, `@action` and plain view.
- It found **546 patterns and 985 endpoints**, the same totals as the inventory.
- It resolved every one of the 1002 requests with Django's `resolve()`. All of them hit an accepted method, and every pattern gets at least one request.
- **No endpoints were missing and no requests were bogus.**
- It found one subtle defect. The two iclock routes shared a variable whose default (`querydata`) is not one of the named route's choices, so the default requests on the named route really resolved to the catch-all route.
- It also found that nine HR/attendance variables had a self-referencing default (`{{x}}` = `"{{x}}"`).

Both were fixed. The fixes also made the checks stronger:
- `verify_coverage.py` now resolves each request's concrete URL the way Django does (first matching pattern in urlconf order) and fails on any **MISROUTE**.
- `build_collection.py` refuses a default that violates a choice-constrained path param.
- `build_collection.py` never emits templated defaults.

**2. 10% random spot-check** (`random.seed(20261001)`, 101 of 1002 requests). This agent resolved each path and read the view, serializer, permissions, filters and success status. It ran `is_valid()` on the request bodies inside a rolled-back transaction.
- **All 7 sampled refused-method requests passed.** DRF's `initial()` (permissions) runs before the `http_method_names` check, so an unmapped `update` gives 403.

It found the following, all fixed:
- **Redirect assertions could never pass.** Postman and Newman follow redirects by default, so the 135 legacy 301 checks never saw the 301. Fix: those requests set `protocolProfileBehavior.followRedirects = false` and also assert the `Location` header.
- **Literal `+` in query examples.** Django decodes `+` as a space, which broke the timezone offsets and phone numbers. Fix: query values are percent-encoded (`%2B`).
- **The legacy job application used `position_id: "1"`.** No `core.LegacyMap` row exists for it, so it returned 400. Fix: it is now sent as a general application, and the description says why.
- **`_Source` pointed at the ViewSet class rather than the action.** For actions that live in a mixin (e.g. `VersionActionsMixin`) the class is in a different file. Fix: `dump_urls.py` now records the handler method's own file and line.
- **Description errors:**
  - the default ordering of reference tariffs (it is `phase, effective_from, slab_from_units, id`);
  - `name` is optional on careers department PATCH;
  - enum lists in descriptions were truncated at 8 values; they now list all values.

Accepted as documented business preconditions, not defects:
- `POST /api/v1/projects/{uid}/lock-bom/` returns 409 `bom_lock_refused` in a fresh linear run, because the active checker rule set blocks a one-line BOM.
- A few later workflow steps can return 409 `stale_version` once an earlier step has changed the row's version.

The request descriptions call these out.

## Smoke run

- `newman` imported the collection and ran every GET in *07 Public website API* and *13 Ops & docs* against the local stack. All URLs and variables resolved, and the test scripts executed.
- The requests that fail without the staff folders running first are data-dependent: content collection, job-position slug, preview token, a media path, and the pack detail (the dev database has no published packs).
- The staff folders were **not** run: no staff password is available. The dev admin's password is set through a one-time link, and it was not reset.

## Code findings reported by the Phase-2 agents (where the code and the OpenAPI schema disagree)

These are recorded for the backend team. The collection follows the code, not the schema.

**Schema is missing parameters or headers the code accepts**
- `DELETE` on `ServiceWritesMixin` ViewSets accepts `?expected_version=` (409 `stale_version`), but the schema doesn't list it. Applies across apps.
- Some `GET` routes accept parameters or headers the schema doesn't show:
  - `GET …/annotations/` accepts `?current=`;
  - public cached routes answer `304` to `If-None-Match`;
  - public lead `status`, `kind` and `form` filters are repeatable (`MultipleChoiceFilter`).

**Schema advertises parameters the views ignore**
- `search` does nothing on several lists that have no `search_fields`:
  - reference tariffs, wattages and room-sizes;
  - pricing installation-matrix, cost-config history and releases;
  - quotation-content versions;
  - blog template image-groups and attribute-slots;
  - devices ADMS requests;
  - public testimonials, which also ignores `ordering`.
- Site-inspections engineer queue: `ordering` is listed, but `ordering_fields = []`.
- Catalog component export: the `format` query param is ignored; the view always returns CSV.

**Schema response shapes differ from the code**
- Responses the code returns as a plain array or a different shape:
  - `GET content/entries/{uid}/slug-history/` returns a plain array; the schema shows a paginated list.
  - Procurement batch `lines/` and `charges/` GET are paginated; the schema shows plain lists.
  - Site-inspection `equipment/` and `additional-work/` lists are plain arrays. `activity/` uses cursor pagination with no `count`.
- `attendance/views/reports.py` defines a `FILE` response for csv and xlsx downloads but never uses it, so the schema only shows JSON.
- `GET devices/{uid}/logs/` lists the device filters, but they only narrow the device lookup, giving 404, rather than the logs.

**Schema declares statuses that can't happen**
- 503 on `devices/{uid}/adms/enable/`.
- 404 on the `emi/settings/` singleton and on `packs/releases/preview/`.
- 400 on bom CRUD `DELETE`.
- 409 on quotation-content testimonial and campaign creates (no unique constraint).

**Real checks that sit in services, so the schema can't show them**
- `site-inspections/{uid}/approval/request/` requires `submit` or `approve`.
- `additional-work/{uid}/transition/` requires `approve`, or `edit` for NONE→IDENTIFIED.
- Leave `cancel/` requires `leave.create` for your own leave, or `leave.approve` for anyone else's.
- `projects/{uid}/cost-inputs/` also requires `pricing_internal.view`.
- Some permissions belong to a different registry module:
  - `run-checker` uses `engineering.verify`;
  - creating an office agent uses `devices.manage`;
  - leave types are read with `leave.view`.

**Code-level inconsistencies worth a look**
- Project create/update `customer_uid` is not checked against the customers record scope, while warranty and lead-convert are.
- `quotations.services.quotations.issue` ignores the `note` field its request serializer accepts.
- `faq-categories` create and PATCH responses always show `faq_count` and `published_faq_count` as 0.
- The site-inspection approval link is a relative path with `v1` hard-coded. `SITE_INSPECTIONS_APPROVAL_LINK_BASE` is not defined in settings.
