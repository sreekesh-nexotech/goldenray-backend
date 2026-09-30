// Shared Playwright helpers for the website UAT: one browser, two origins (legacy / platform), per-page capture.
import { chromium } from "/opt/node22/lib/node_modules/playwright/index.mjs";
import fs from "node:fs";
import path from "node:path";

export const ORIGINS = { legacy: "http://127.0.0.1:18320", platform: "http://127.0.0.1:18310" };
// Captures go to $UAT_DIR/results (UAT_DIR = the scratch folder holding the website copies, nginx configs and logs).
export const OUT = path.join(process.env.UAT_DIR || process.cwd(), "results");
fs.mkdirSync(OUT, { recursive: true });

const API_RE = /^\/(api|studio-api|bom)\//;

export async function launch() {
  process.env.PLAYWRIGHT_BROWSERS_PATH = "/opt/pw-browsers";
  return chromium.launch({ headless: true });
}

/** Attach collectors to a page; returns the live record. */
export function instrument(page, origin) {
  const rec = { console: [], pageErrors: [], failed: [], httpErrors: [], api: [] };
  page.on("console", (m) => {
    if (m.type() === "error" || m.type() === "warning") rec.console.push({ type: m.type(), text: m.text().slice(0, 500) });
  });
  page.on("pageerror", (e) => rec.pageErrors.push(String(e).slice(0, 500)));
  page.on("requestfailed", (r) => {
    const f = r.failure()?.errorText || "";
    if (f.includes("ERR_ABORTED")) return; // navigation-cancelled prefetches
    rec.failed.push({ url: rel(r.url(), origin), error: f });
  });
  page.on("response", async (res) => {
    const req = res.request();
    const u = new URL(res.url());
    const local = u.origin === origin;
    if (res.status() >= 400 && !(local && u.pathname.startsWith("/_next/"))) {
      rec.httpErrors.push({ url: rel(res.url(), origin), status: res.status(), method: req.method() });
    }
    if (local && API_RE.test(u.pathname)) {
      let body = null;
      try {
        body = await res.text();
      } catch {
        body = null;
      }
      rec.api.push({ method: req.method(), url: u.pathname + u.search, status: res.status(), request: req.postData() || null, body });
    }
  });
  return rec;
}

export function rel(url, origin) {
  return url.startsWith(origin) ? url.slice(origin.length) : url;
}

export async function settle(page) {
  try {
    await page.waitForLoadState("networkidle", { timeout: 15000 });
  } catch {}
  // Scroll through the page so lazy sections and intersection observers fire.
  await page.evaluate(async () => {
    const step = 600;
    for (let y = 0; y < document.body.scrollHeight; y += step) {
      window.scrollTo(0, y);
      await new Promise((r) => setTimeout(r, 120));
    }
    window.scrollTo(0, 0);
  });
  try {
    await page.waitForLoadState("networkidle", { timeout: 15000 });
  } catch {}
  await page.waitForTimeout(500);
}

export async function snapshot(page) {
  return page.evaluate(() => {
    const ld = [...document.querySelectorAll('script[type="application/ld+json"]')].map((s) => {
      try {
        return JSON.parse(s.textContent);
      } catch {
        return s.textContent;
      }
    });
    const meta = {};
    for (const m of document.querySelectorAll("meta[name],meta[property]")) meta[m.getAttribute("name") || m.getAttribute("property")] = m.getAttribute("content");
    const canonical = document.querySelector('link[rel="canonical"]')?.href || null;
    return { title: document.title, text: document.body.innerText, ld, meta, canonical, url: location.pathname + location.search };
  });
}

export function save(name, data) {
  fs.writeFileSync(path.join(OUT, name), JSON.stringify(data, null, 1));
}

export async function shot(page, name) {
  const dir = path.join(OUT, "shots");
  fs.mkdirSync(dir, { recursive: true });
  await page.screenshot({ path: path.join(dir, name), fullPage: true });
}
