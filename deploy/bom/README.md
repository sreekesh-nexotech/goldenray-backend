# Seed data

The Flarize `data/` documents the platform is seeded from, kept in the repo so a
deployment can configure itself without anyone hand-running a shell snippet.

`import_flarize --source-dir` reads a flat directory of JSON documents by **file
name**: each `*.json` is one table holding one document, and a file it does not
recognise is simply never read. So this directory can hold exactly the files the
steps below need and nothing else has to change.

## Seeding a fresh deployment

Always dry-run first — it prints the counts and every violation, and writes
nothing:

```bash
python manage.py import_flarize --source-dir deploy/bom --only flarize.documents --dry-run
```

Then for real, naming a person so the audit log is not anonymous:

```bash
python manage.py import_flarize --source-dir deploy/bom --only flarize.documents --actor you@example.com
```

| Step | Files it reads | What it writes |
|---|---|---|
| `flarize.documents` | `cost-config.json`, `project-rate-card.json`, `quotation-policy.json`, `energy-config.json`, `savings-config.json`, `subsidy-config.json`, `finance-config.json` | Cost config (incl. **`energy.config`**, the electricity tariff), the rate card, the validity policy |
| `flarize.catalog_pricing` | `catalog.json` | Catalog rows, LIST prices, cost config, the installation matrix, **market rates** (into a DRAFT set), offers |
| `flarize.bom` | `catalog.json` | Structure templates, tube weights, BOM templates, package profiles |

`--list-steps` prints the rest.

Every import is **idempotent**: re-running over unchanged data reports
`unchanged`, never duplicates. Cost config is append-only, so a changed value
opens a new dated row and closes the previous one rather than overwriting it.

## Two things that are not automatic

**Market rates land in a DRAFT set, never ACTIVE.** The quote engine reads only
the ACTIVE set, so freshly imported rates are priced as if they did not exist and
every quote falls back to cost-plus until someone activates them. That is
deliberate — importing prices should not change what customers are charged.
Activate from Studio → BOM Admin → Market rates, which needs
`market_rates.publish`.

**The tariff here is a starting point, not an authority.** `energy-config.json`
carries KSEB's LT-1A slabs as captured when this was written. A tariff order
changes; this file does not follow it. After seeding, check the figures in
Studio → BOM Admin → Tariff against the current KSERC order — the worked example
on that tab shows what each bill becomes, so a stale slab shows up as a wrong
recommended size rather than as a wrong digit.

Known divergence at the time of writing: these slabs size a ₹3,000 bi-monthly
bill to **1.9 kW**, while the Golden-Ray-BOM tool's own hardcoded table gives
**1.3 kW**. The platform's configured value is authoritative by decision — a
tariff in configuration can be corrected without a release, a hardcoded one goes
stale silently — but *authoritative* is not *current*, and someone should confirm
which one matches the order KSEB is actually billing under.

## Editing these files

They are the seed, not the live configuration. Changing a file here does nothing
until an import runs, and an import will not overwrite a value an admin has since
changed in the Studio without saying so: conflicts are reported as
`d2_flarize_wins` warnings naming both figures, so a surprising price change is
visible in the run output rather than silent.

`catalog.json` had the source tool's shared `adminPassword` removed before it was
committed. Nothing in the platform ever read it, and a plaintext credential in a
repository is worth deleting even when it is inert.
