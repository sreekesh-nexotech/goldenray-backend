// Interactive flows, run identically on both copies. Usage: node flows.mjs [flowNameFilter]
// Phone numbers are synthetic test numbers (never real subscribers); the platform runs the fake OTP backend and the
// private legacy server has no outbound network, so no SMS can be sent by either side.
import { launch, instrument, settle, snapshot, save, shot, ORIGINS, OUT } from "./lib.mjs";
import fs from "node:fs";

const RUN = process.env.UAT_RUN || "1";
// Per-run test numbers (10 digits, 9000xxxxxx block), distinct per side so each backend sees a fresh number.
const phone = (side, n) => `90${side.startsWith("legacy") ? "1" : "2"}${RUN.padStart(2, "0")}${String(n).padStart(5, "0")}`;

async function clickText(page, text, opts = {}) {
  await page.getByText(text, { exact: opts.exact ?? true }).first().click({ timeout: opts.timeout ?? 10000 });
}

async function waitApi(page, re, timeout = 30000) {
  try {
    await page.waitForResponse((r) => re.test(new URL(r.url()).pathname), { timeout });
  } catch {}
}

export const FLOWS = {
  // Home basic calculator → results → "Get quote" popup → lead + BOM + PDF generation
  async home_calculator_quote(page, side) {
    await page.goto("/", { waitUntil: "domcontentloaded" });
    await settle(page);
    await page.fill("#pincode", "688503");
    await page.selectOption("#property-type", "residential");
    await page.fill("#electricity-bill", "2500");
    const r = waitApi(page, /calculate-solar-new/);
    await page.locator("#pincode").locator("xpath=ancestor::form").locator('button[type="submit"]').click();
    await r;
    await page.waitForTimeout(1500);
    // resubmit with another bill from the result form
    if (await page.locator("#electricity-bill-result").count()) {
      await page.fill("#electricity-bill-result", "4200");
      const r2 = waitApi(page, /calculate-solar-new/);
      await page.locator("#electricity-bill-result").locator("xpath=ancestor::*[.//button][1]").locator("button").first().click();
      await r2;
      await page.waitForTimeout(1000);
    }
    const mid = await snapshot(page);
    // open the quote popup (button under the result)
    const btn = page.locator("#solar-advantage-results button").filter({ hasText: /quote|Quotation/i }).first();
    await btn.click({ timeout: 10000 });
    await page.fill("#customerName", "UAT Tester");
    await page.fill("#address", "1 Test Street, Alappuzha");
    await page.fill("#phoneNumber", phone(side, 1));
    await page.selectOption("#preferredLanguage", "English");
    await page.selectOption("#subsidyEligibility", { index: 1 });
    const lead = waitApi(page, /lead-collection-home/);
    const bom = waitApi(page, /\/bom\/api\/calculate/, 60000);
    const dl = page.waitForEvent("download", { timeout: 120000 }).catch(() => null);
    await page.locator('#customerName').locator("xpath=ancestor::form").locator('button[type="submit"]').click();
    await lead;
    await bom;
    const download = await dl;
    return { mid, download: download ? download.suggestedFilename() : null };
  },

  async home_calculator_unknown_pincode(page) {
    await page.goto("/", { waitUntil: "domcontentloaded" });
    await settle(page);
    await page.fill("#pincode", "110001");
    await page.selectOption("#property-type", "commercial");
    await page.fill("#electricity-bill", "9000");
    const r = waitApi(page, /calculate-solar-new/);
    await page.locator("#pincode").locator("xpath=ancestor::form").locator('button[type="submit"]').click();
    await r;
    await page.waitForTimeout(1500);
  },

  // Quotation v2 pages rendered from the data the popup stored (settings, testimonials, EMI quotation)
  async quotation_pages(page, side) {
    await FLOWS.home_calculator_quote(page, side);
    const out = {};
    for (const p of ["/quotation/v2?variant=full", "/quotation/v2-malayalam?variant=full", "/quotation"]) {
      await page.goto(p, { waitUntil: "domcontentloaded" });
      await settle(page);
      out[p] = await snapshot(page);
    }
    return out;
  },

  async advanced_ongrid_existing_otp(page, side) {
    await page.goto("/advanced-calculator", { waitUntil: "domcontentloaded" });
    await settle(page);
    await clickText(page, "Existing Home");
    await clickText(page, "On Grid");
    await page.fill('input[name="average_bill"]', "6000");
    await clickText(page, "Bi-monthly");
    await page.getByRole("button", { name: "Next" }).click();
    await page.waitForTimeout(1500);
    const calc = waitApi(page, /calculate-solar-advanced/);
    await page.getByRole("button", { name: /Calculate/ }).first().click();
    await calc;
    await page.waitForTimeout(2000);
    const result = await snapshot(page);
    await page.getByRole("button", { name: "Get Detailed Quote" }).click();
    await page.fill("#name", "UAT Tester");
    await page.fill("#phoneNumber", phone(side, 2));
    const send = waitApi(page, /send-otp/);
    await page.locator("#phoneNumber").locator("xpath=ancestor::div[.//button][1]").getByRole("button").last().click();
    await send;
    await page.waitForTimeout(1000);
    const afterSend = await snapshot(page);
    if (await page.locator("#code").count()) {
      await page.fill("#code", "000000");
      const ver = waitApi(page, /verify-otp/);
      await page.locator("#code").locator("xpath=ancestor::div[.//button][1]").getByRole("button").last().click();
      await ver;
      await page.waitForTimeout(1000);
    }
    return { result, afterSend };
  },

  async advanced_hybrid_newhome(page) {
    await page.goto("/advanced-calculator", { waitUntil: "domcontentloaded" });
    await settle(page);
    await clickText(page, "New Home");
    await clickText(page, "Hybrid");
    await page.waitForTimeout(800);
    await page.selectOption("#home_size", { index: 2 });
    await page.getByRole("button", { name: "Next" }).click();
    await page.waitForTimeout(1500);
    // step 2: add an EV (vehicle manager) if present, then continue
    const step2 = await snapshot(page);
    await page.getByRole("button", { name: /Next|Continue|Calculate/ }).last().click();
    await page.waitForTimeout(1500);
    const bh = page.locator('input[name="backup_hours"]');
    if (await bh.count()) await bh.fill("4");
    const calc = waitApi(page, /calculate-solar-advanced/);
    await page.getByRole("button", { name: /Calculate/ }).last().click();
    await calc;
    await page.waitForTimeout(2000);
    return { step2 };
  },

  async emi_calculator(page) {
    await page.goto("/emi-calculator", { waitUntil: "domcontentloaded" });
    await settle(page);
    const states = {};
    const tiles = page.locator("#calculator button").filter({ hasText: /kW/ });
    const n = await tiles.count();
    for (let i = 0; i < n; i++) {
      const w = waitApi(page, /emi-calculator\/$/, 8000);
      await tiles.nth(i).click();
      await w;
      await page.waitForTimeout(700);
      states[`tile${i}`] = (await snapshot(page)).text.slice(0, 4000);
    }
    const tog = page.locator('[aria-label="Toggle With/Without Subsidy"]');
    if (await tog.count()) {
      const w = waitApi(page, /emi-calculator\/$/, 8000);
      await tog.first().click();
      await w;
      await page.waitForTimeout(700);
    }
    return { states };
  },

  async contact_form(page, side) {
    await page.goto("/contactus", { waitUntil: "domcontentloaded" });
    await settle(page);
    await page.fill("#name", "UAT Contact");
    await page.fill("#phone_number", phone(side, 3));
    await page.fill("#pin_code", "688503");
    await page.selectOption("#property_type", "Residential");
    const r = waitApi(page, /lead-collection-home/);
    await page.locator("#name").locator("xpath=ancestor::form").locator('button[type="submit"]').click();
    await r;
    await page.waitForTimeout(1500);
  },

  async contact_form_invalid(page) {
    await page.goto("/contactus", { waitUntil: "domcontentloaded" });
    await settle(page);
    await page.fill("#name", "UAT Contact");
    await page.fill("#phone_number", "12345");
    await page.locator("#name").locator("xpath=ancestor::form").locator('button[type="submit"]').click();
    await page.waitForTimeout(2000);
  },

  async affiliate_form(page, side) {
    await page.goto("/solar-referral-program", { waitUntil: "domcontentloaded" });
    await settle(page);
    await page.fill("#full_name", "UAT Partner");
    await page.fill("#phone", phone(side, 4));
    await page.fill("#email", "uat.partner@example.com");
    await page.selectOption("#profession", { index: 1 });
    await page.selectOption("#district", { index: 1 });
    const r = waitApi(page, /affiliate-applications/);
    await page.locator("#full_name").locator("xpath=ancestor::form").locator('button[type="submit"]').click();
    await r;
    await page.waitForTimeout(1500);
  },

  async warranty_form(page, side) {
    await page.goto("/solar-warranty", { waitUntil: "domcontentloaded" });
    await settle(page);
    const form = page.locator("#contact form").first();
    await form.locator('input[placeholder="Enter your name"]').fill("UAT Owner");
    await form.locator('input[placeholder="Enter your number"]').fill(phone(side, 5));
    const sel = form.locator("select");
    for (let i = 0; i < (await sel.count()); i++) await sel.nth(i).selectOption({ index: 1 });
    await form.locator("textarea").fill("Inverter shows a grid fault after rain.");
    if (side.endsWith("honeypot")) await form.locator('input[tabindex="-1"]').first().fill("http://spam.example");
    const r = waitApi(page, /warranty-service-requests/);
    await form.locator('button[type="submit"]').click();
    await r;
    await page.waitForTimeout(1500);
  },

  // a bot filling the hidden honeypot field is refused identically
  async warranty_form_honeypot(page, side) {
    return FLOWS.warranty_form(page, side + "-honeypot");
  },

  async career_apply_position(page, side) {
    await page.goto("/career/solar-installation-engineer", { waitUntil: "domcontentloaded" });
    await settle(page);
    await fillApplication(page, side, 6, "UAT Applicant", "uat.applicant@example.com", "Kochi");
  },

  async career_apply_general(page, side) {
    await page.goto("/career/general-application-form", { waitUntil: "domcontentloaded" });
    await settle(page);
    await fillApplication(page, side, 7, "UAT General", "uat.general@example.com", "Thrissur");
  },

  async career_apply_invalid(page) {
    await page.goto("/career/crs-executive", { waitUntil: "domcontentloaded" });
    await settle(page);
    await page.locator('form button[type="submit"]').last().click();
    await page.waitForTimeout(1500);
  },

  async footer_callback(page, side) {
    await page.goto("/about", { waitUntil: "domcontentloaded" });
    await settle(page);
    const form = page.locator("footer form").first();
    await form.locator('input[name="name"]').fill("UAT Footer");
    await form.locator('input[name="phone_number"]').fill(phone(side, 8));
    const r = waitApi(page, /lead-collection-home/);
    await form.locator('button[type="submit"]').click();
    await r;
    await page.waitForTimeout(1500);
  },

  async home_booking(page, side) {
    await page.goto("/", { waitUntil: "domcontentloaded" });
    await settle(page);
    const form = page.locator("#booking form").first();
    await form.locator('input[name="name"]').fill("UAT Booking");
    await form.locator('input[name="phone_number"]').fill(phone(side, 9));
    const r = waitApi(page, /lead-collection-home/);
    await form.locator('button[type="submit"]').click();
    await r;
    await page.waitForTimeout(1500);
  },

  async group_purchase_reserve(page, side) {
    await page.goto("/group-purchase", { waitUntil: "domcontentloaded" });
    await settle(page);
    const f = page.locator("#reserve");
    await f.locator("#name").fill("UAT Group");
    await f.locator("#phone").fill(phone(side, 10));
    await f.locator("#email").fill("uat.group@example.com");
    for (const id of ["#role", "#district", "#estimate", "#bill"]) {
      const s = f.locator(id);
      if ((await s.count()) && (await s.evaluate((e) => e.tagName)) === "SELECT") await s.selectOption({ index: 1 });
    }
    await f.locator("#locality").fill("Mannancherry");
    await f.locator("#group-name").fill("UAT solar group");
    const r = waitApi(page, /lead-collection-home/);
    await f.locator('button[type="submit"]').click();
    await r;
    await page.waitForTimeout(1500);
  },

  // Same number asks for a code repeatedly: legacy 30-day block (429 + message) vs platform throttle/daily cap.
  async otp_repeat_send(page, side) {
    const out = [];
    for (let i = 0; i < Number(process.env.UAT_OTP_TRIES || 6); i++) {
      await page.goto("/advanced-calculator", { waitUntil: "domcontentloaded" });
      await settle(page);
      await clickText(page, "Existing Home");
      await clickText(page, "On Grid");
      await page.fill('input[name="average_bill"]', "6000");
      await clickText(page, "Bi-monthly");
      await page.getByRole("button", { name: "Next" }).click();
      await page.waitForTimeout(1200);
      const calc = waitApi(page, /calculate-solar-advanced/);
      await page.getByRole("button", { name: /Calculate/ }).first().click();
      await calc;
      await page.waitForTimeout(1200);
      await page.getByRole("button", { name: "Get Detailed Quote" }).click();
      await page.fill("#name", "UAT Repeat");
      await page.fill("#phoneNumber", phone(side, 11));
      const send = waitApi(page, /send-otp/);
      await page.locator("#phoneNumber").locator("xpath=ancestor::div[.//button][1]").getByRole("button").last().click();
      await send;
      await page.waitForTimeout(800);
      const txt = (await snapshot(page)).text;
      const m = txt.match(/[^\n]*(try again|went wrong|Verify OTP)[^\n]*/i);
      out.push(m ? m[0] : "(no message)");
    }
    return { attempts: out };
  },

  async solar_comparison_filters(page) {
    await page.goto("/solar-comparison", { waitUntil: "domcontentloaded" });
    await settle(page);
    const out = {};
    const selects = page.locator("main select");
    const n = await selects.count();
    for (let i = 0; i < n; i++) {
      const opts = await selects.nth(i).locator("option").count();
      if (opts > 1) {
        const w = waitApi(page, /solar-panels/, 6000);
        await selects.nth(i).selectOption({ index: Math.min(2, opts - 1) });
        await w;
        await page.waitForTimeout(800);
        out[`select${i}`] = (await snapshot(page)).text.slice(0, 3000);
      }
    }
    // compare two panels
    const cmp = page.getByText("Compare", { exact: true });
    if ((await cmp.count()) >= 2) {
      await cmp.nth(0).click();
      await cmp.nth(1).click();
      await page.waitForTimeout(800);
      const go = page.getByRole("button", { name: /Compare Now|Compare \(|View Comparison/i });
      if (await go.count()) {
        await go.first().click();
        await page.waitForLoadState("domcontentloaded");
        await settle(page);
      }
    }
    return out;
  },

  async comparison_table_ids(page) {
    await page.goto("/comparison-table?ids=1,2,3", { waitUntil: "domcontentloaded" });
    await settle(page);
  },

  async inverter_comparison_filters(page) {
    await page.goto("/inverter-comparison", { waitUntil: "domcontentloaded" });
    await settle(page);
    const selects = page.locator("main select");
    const n = await selects.count();
    for (let i = 0; i < n; i++) {
      const opts = await selects.nth(i).locator("option").count();
      if (opts > 1) {
        const w = waitApi(page, /solar-inverters/, 6000);
        await selects.nth(i).selectOption({ index: Math.min(2, opts - 1) });
        await w;
        await page.waitForTimeout(800);
      }
    }
    await page.goto("/inverter-comparison-table?ids=1,2", { waitUntil: "domcontentloaded" });
    await settle(page);
  },
};

async function fillApplication(page, side, n, name, email, location) {
  const form = page.locator("form").filter({ has: page.locator('input[type="file"]') }).first();
  await form.locator('input[placeholder="Enter your name"]').fill(name);
  await form.locator('input[placeholder="Enter email id"]').fill(email);
  await form.locator('input[placeholder="Enter number"]').fill(phone(side, n));
  await form.locator('input[placeholder="Enter current location"]').fill(location);
  await form.locator('input[placeholder="Paste URL"]').first().fill("https://www.linkedin.com/in/uat-applicant");
  const sel = form.locator("select");
  for (let i = 0; i < (await sel.count()); i++) await sel.nth(i).selectOption({ index: 1 });
  const cur = form.locator('input[placeholder="Enter your current company"]');
  if (await cur.count()) await cur.fill("UAT Co");
  const role = form.locator('input[placeholder="Enter your current Role"]');
  if (await role.count()) await role.fill("Engineer");
  const ta = form.locator("textarea");
  if (await ta.count()) await ta.first().fill("Solar installation experience across Kerala rooftops.");
  await form.locator('input[type="file"]').first().setInputFiles({
    name: "resume.pdf", mimeType: "application/pdf",
    buffer: Buffer.from("%PDF-1.4\n1 0 obj<</Type/Catalog/Pages 2 0 R>>endobj 2 0 obj<</Type/Pages/Count 0/Kids[]>>endobj\ntrailer<</Root 1 0 R>>\n%%EOF\n"),
  });
  await form.locator('input[type="checkbox"]').last().check();
  const r = waitApi(page, /job-applications/);
  await form.locator('button[type="submit"]').click();
  await r;
  await page.waitForTimeout(1500);
}

const filter = process.argv[2];
const browser = await launch();
for (const [name, fn] of Object.entries(FLOWS)) {
  if (filter && !name.includes(filter)) continue;
  // UAT_SIDES=platform (or legacy) re-captures one side and keeps the other side's earlier capture
  const only = (process.env.UAT_SIDES || "legacy,platform").split(",");
  const prev = `${OUT}/flow_${name}.json`;
  const out = fs.existsSync(prev) ? JSON.parse(fs.readFileSync(prev, "utf8")) : { flow: name };
  for (const [side, origin] of Object.entries(ORIGINS).filter(([s]) => only.includes(s))) {
    out[side] = {};
    const ctx = await browser.newContext({ baseURL: origin, viewport: { width: 1366, height: 900 }, acceptDownloads: true });
    const page = await ctx.newPage();
    page.on("dialog", (d) => {
      (out[side] ??= {}).dialogs = [...((out[side] ?? {}).dialogs || []), d.message()];
      d.dismiss().catch(() => {});
    });
    const rec = instrument(page, origin);
    let extra = null;
    let error = null;
    try {
      extra = await fn(page, side);
    } catch (e) {
      error = String(e).split("\n")[0];
    }
    let snap = {};
    try {
      snap = await snapshot(page);
    } catch {}
    await shot(page, `${name}.${side}.png`).catch(() => {});
    out[side] = { ...(out[side] || {}), error, ...snap, extra, ...rec };
    await ctx.close();
  }
  save(`flow_${name}.json`, out);
  console.log(name, "legacy:", out.legacy?.error || "ok", "| platform:", out.platform?.error || "ok");
}
await browser.close();
