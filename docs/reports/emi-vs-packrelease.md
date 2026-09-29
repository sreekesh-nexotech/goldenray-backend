# EMI system sizes: legacy `emi_system_size` prices vs PackRelease #1 (PLAN §7.3)

PLAN §7.3 says `emi_system_size` is not migrated and asks for a report of "the price it held vs. the PackRelease price
for the same size". DV-83 keeps the rows as the transitional `MANUAL` price source; this report is what the business
reviews before switching `EMI_PRICE_SOURCE` to `PACK_RELEASE`.

## Sources

* **Legacy**: the four `emi_system_size` rows of the GoldenApp database, as captured in
  `emi/tests/parity/uat/legacy_rows.json` (seeded by the legacy migrations `0054_seed_emi_calculator_config`,
  `0056_emi_system_cost_bands`, `0062_emi_price_max_ceilings`). The legacy calculator prices a tile at
  `price_per_kw × capacity_kw`. The extra rows in `emi/tests/parity/enriched/` (4 kW, 6.5 kW, a hidden 12 kW) are
  synthetic test rows, so they are left out.
* **PackRelease #1**: produced from the real Flarize data by the parity test
  (`packs/tests/test_release_parity.py`: catalog → pricing → bom → packs imports, then `publish_initial_releases()`).
  That gives PriceRelease #1 and pack config v16, with 11 of 90 packs sellable and 79 left out with the Flarize reason
  (mostly `MARKET_RATE_NOT_SET`). Prices are the released customer price including GST (the FLAT-roof base price
  without transport, DV-100).
* **What the EMI calculator would show** under `PACK_RELEASE`: `emi.services.pack_release.release_sizes()` gives one
  tile for each standard on-grid pack. Future-ready packs are left out. The tile's `system_cost` is the pack price and
  `price_per_kw` is that price divided by the size. `emi/tests/test_pack_release.py` checks this.

## Legacy tiles

| Size | Legacy per kW | Legacy system cost | Slider min | Slider max | Monthly bill ref. |
|---|---:|---:|---:|---:|---:|
| 3 kW | 76,667 | 230,001 | 180,000 | 500,000 | 6,000 |
| 5 kW | 66,000 | 330,000 | 260,000 | 650,000 | 10,000 |
| 8 kW | 65,625 | 525,000 | 420,000 | 750,000 | 15,500 |
| 10 kW | 60,000 | 600,000 | 480,000 | 900,000 | 20,000 |

## Same size, side by side (₹, incl. GST)

| Size | PackRelease #1 pack (EMI tile uid) | Pack price | Pack per kW | Legacy system cost | Difference | Difference % |
|---|---|---:|---:|---:|---:|---:|
| 3 kW | `ongrid-base-3` (On-grid 3 kW Base) | 200,000 | 66,666.67 | 230,001 | −30,001 | −13.04 % |
| 3 kW | `ongrid-value-3` (On-grid 3 kW Value) | 229,000 | 76,333.33 | 230,001 | −1,001 | −0.44 % |
| 3 kW | `ongrid-premium-3` (On-grid 3 kW Premium) | 300,000 | 100,000.00 | 230,001 | +69,999 | +30.43 % |
| 5 kW | `ongrid-base-5sp` (On-grid 5kW 1P Base) | 305,000 | 61,000.00 | 330,000 | −25,000 | −7.58 % |
| 5 kW | `ongrid-value-5sp` (On-grid 5kW 1P Value) | 330,000 | 66,000.00 | 330,000 | 0 | 0.00 % |
| 5 kW | `ongrid-premium-5sp` (On-grid 5kW 1P Premium) | 400,000 | 80,000.00 | 330,000 | +70,000 | +21.21 % |
| 5 kW | `ongrid-base-5tp` (On-grid 5kW 3P Base) | 330,000 | 66,000.00 | 330,000 | 0 | 0.00 % |
| 5 kW | `ongrid-value-5tp` (On-grid 5kW 3P Value) | 350,000 | 70,000.00 | 330,000 | +20,000 | +6.06 % |
| 5 kW | `ongrid-premium-5tp` (On-grid 5kW 3P Premium) | 410,000 | 82,000.00 | 330,000 | +80,000 | +24.24 % |
| 8 kW | — (no sellable pack in PackRelease #1) | — | — | 525,000 | — | — |
| 10 kW | — (no sellable pack in PackRelease #1) | — | — | 600,000 | — | — |

These packs are released but are not EMI tiles because they are future-ready: `ongrid-base-3-up5sp` and
`ongrid-value-3-up5sp`, both 245,000.

## Findings

1. **The mid tier matches the legacy tile.** The Value packs are within 0.44 % of the legacy prices: 3 kW 229,000 vs
   230,001, and 5 kW 1P 330,000 vs 330,000. The 5 kW 3P Base pack also matches at 330,000. The legacy tile has a
   single price for each size. A release has up to three tiers per size, and two phases at 5 kW, so the calculator
   would show 3 tiles for 3 kW and 6 tiles for 5 kW where it showed 1 each.
2. **8 kW and 10 kW would disappear.** PackRelease #1 has no sellable on-grid 8 kW or 10 kW pack: the market rates are
   not set, so the pack is left out with `MARKET_RATE_NOT_SET`. Under `PACK_RELEASE` the EMI calculator would offer only
   3 kW and 5 kW. Until those market rates are published, `MANUAL` stays the right source.
3. **The 3 kW Value price depends on an open business confirmation.** The legacy import lets the approved pack
   config's market rate win over `catalog.json`, which is reported as `pack_config_market_rate_wins`. That makes
   `ongrid_value/3` 229,000 instead of 228,000. With 228,000 the difference would be −2,001 (−0.87 %).
4. **Every pack price is inside the legacy slider bounds** for its size: 3 kW is 180,000 – 500,000 and 5 kW is
   260,000 – 650,000. Under `PACK_RELEASE` the tiles carry no `price_min`/`price_max` of their own.
5. **Monthly-bill reference.** Pack tiles carry `monthly_bill_reference = 0`, while the legacy tiles carry
   6,000 / 10,000 / 15,500 / 20,000. `engines.emi.calculate` works out `monthly_savings` as that bill minus the EMI,
   so under `PACK_RELEASE` the savings figure would come out negative. Before the switch the reference needs a source,
   for example a column on the pack or an EMI-side table keyed by size. This is listed as an open item.

## Recommendation

Keep `EMI_PRICE_SOURCE=MANUAL`, which is the default, until a PackRelease has sellable on-grid packs for every size the
calculator offers (at least 8 kW and 10 kW) and the business has decided three things:

* whether EMI shows every tier or only one per size (Value is the like-for-like choice);
* the 229,000 vs 228,000 market rate for `ongrid_value/3`;
* where the monthly-bill reference comes from.
