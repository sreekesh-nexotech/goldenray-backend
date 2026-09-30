// Page crawl: every public route on both copies; captures text, JSON-LD, meta, console errors, failed requests and
// every API response the browser saw. Usage: node crawl.mjs [filter]
import fs from "node:fs";

import { launch, instrument, settle, snapshot, save, ORIGINS, OUT } from "./lib.mjs";

export const PAGES = [
  "/", "/about", "/advanced-calculator", "/blog", "/career", "/career/general-application-form", "/commercial",
  "/comparison-table", "/contactus", "/emi-calculator", "/faq", "/group-purchase", "/how-flarize-works", "/industrial",
  "/inverter-comparison", "/inverter-comparison-table", "/privacy", "/projects", "/quote-analyser", "/residential",
  "/resources", "/solar-comparison", "/solar-referral-program", "/solar-warranty", "/solutions", "/subsidy", "/terms",
  "/quotation", "/quotation/v2", "/quotation/v2-malayalam",
  // CMS-driven dynamic pages
  "/blog/pm-surya-ghar-subsidy-guide", "/blog/on-grid-vs-hybrid-kerala", "/blog/how-net-metering-works",
  "/blog/solar-panel-cleaning", "/blog/battery-sizing-guide", "/blog/net-metering-explained", "/blog/draft-inverter-guide",
  "/blog/archived-old-subsidy", "/blog/does-not-exist",
  "/career/solar-installation-engineer", "/career/field-sales-executive", "/career/crs-executive",
  "/career/site-supervisor-intern", "/career/store-keeper-closed", "/career/design-engineer-draft",
  "/career/field-sales-lead", "/career/ui-ux-designer",
  // shipped-data pages
  "/projects/jose-vp-vadackkal-alappuzha", "/resources/solar-panel-maintenance-tips",
  "/service-area/alappuzha", "/service-area/ernakulam", "/service-area/kannur",
  // redirects, 404, SEO files
  "/solar-faq", "/solar-calculator", "/aboutus", "/no-such-page", "/sitemap.xml", "/sitemap-0.xml", "/robots.txt",
];

const filter = process.argv[2];
const browser = await launch();
const [lo, hi] = /^\d+:\d+$/.test(filter || "") ? filter.split(":").map(Number) : [0, PAGES.length];
for (const p of PAGES.slice(lo, hi).filter((x) => !filter || /^\d+:\d+$/.test(filter) || x.includes(filter))) {
  // UAT_SIDES=platform (or legacy) captures one side and keeps the other side's earlier capture
  const only = (process.env.UAT_SIDES || "legacy,platform").split(",");
  const name = "page" + (p === "/" ? "_home" : p.replace(/[^a-z0-9]+/gi, "_")) + ".json";
  const prev = `${OUT}/${name}`;
  const out = fs.existsSync(prev) ? JSON.parse(fs.readFileSync(prev, "utf8")) : {};
  delete out.path;
  for (const [side, origin] of Object.entries(ORIGINS).filter(([s]) => only.includes(s))) {
    const ctx = await browser.newContext({ viewport: { width: 1366, height: 900 } });
    const page = await ctx.newPage();
    const rec = instrument(page, origin);
    let status = null;
    try {
      const resp = await page.goto(origin + p, { waitUntil: "domcontentloaded", timeout: 60000 });
      status = resp?.status() ?? null;
      await settle(page);
      out[side] = { status, ...(await snapshot(page)), ...rec };
    } catch (e) {
      out[side] = { status, error: String(e), ...rec };
    }
    await ctx.close();
  }
  save(name, { path: p, ...out });
  console.log(p, out.legacy?.status, out.platform?.status);
}
await browser.close();
