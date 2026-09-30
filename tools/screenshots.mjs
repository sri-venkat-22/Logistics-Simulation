// Drives the clickable prototype like a user and captures every §6.5 screen for the SRS.
// Usage: node screenshots.mjs [baseUrl] [outDir]
import puppeteer from "puppeteer-core";
import { mkdirSync } from "node:fs";
import { resolve } from "node:path";

const BASE = process.argv[2] ?? "http://localhost:5173/";
const OUT = resolve(process.argv[3] ?? "../docs/wireframes");
const CHROME = process.env.CHROME_PATH ?? "/Applications/Google Chrome.app/Contents/MacOS/Google Chrome";
mkdirSync(OUT, { recursive: true });

const sleep = (ms) => new Promise((r) => setTimeout(r, ms));

const browser = await puppeteer.launch({
  executablePath: CHROME,
  headless: true,
  args: ["--ignore-gpu-blocklist", "--enable-gpu", "--use-angle=metal", "--window-size=1440,900", "--hide-scrollbars"],
  defaultViewport: { width: 1440, height: 900, deviceScaleFactor: 1.5 },
});
const page = await browser.newPage();
const errors = [];
page.on("pageerror", (e) => errors.push(String(e)));
page.on("console", (m) => { if (m.type() === "error") errors.push(m.text()); });

async function go(hash, wait = 3500) {
  await page.goto(`${BASE}#${hash}`, { waitUntil: "networkidle2", timeout: 60000 }).catch(() => {});
  await sleep(wait);
}
async function key(k, wait = 600) { await page.keyboard.press(k); await sleep(wait); }
async function clickText(text, wait = 800, selector = "button") {
  const ok = await page.evaluate((t, sel) => {
    const el = [...document.querySelectorAll(sel)].find((b) => b.textContent?.includes(t) && !b.disabled);
    if (el) { el.click(); return true; }
    return false;
  }, text, selector);
  if (!ok) errors.push(`clickText: "${text}" not found`);
  await sleep(wait);
}
async function shot(name) {
  await page.screenshot({ path: `${OUT}/${name}.png` });
  console.log(`  ${name}.png`);
}

console.log(`capturing ${BASE} → ${OUT}`);
await go("/intro", 1800); await shot("01-intro-hook");
await sleep(6800); await shot("02-intro-tagline");

await go("/", 6000); await shot("03-control-tower");
await page.keyboard.down("Meta"); await page.keyboard.press("k"); await page.keyboard.up("Meta"); await sleep(500);
await page.keyboard.type("Shamshabad"); await sleep(300); await shot("04-command-palette");
await key("Enter", 1200); await shot("05-control-tower-node");
await key("Escape", 400);
await page.evaluate(() => {
  const el = document.querySelector('input[aria-label="Timeline offset in hours"]');
  const set = Object.getOwnPropertyDescriptor(HTMLInputElement.prototype, "value").set;
  set.call(el, "84"); el.dispatchEvent(new Event("input", { bubbles: true }));
});
await sleep(1500); await shot("06-control-tower-future");

await go("/scenario", 3000); await key("d", 1500); await shot("07-scenario-placed");
await clickText("Run 500", 1400); await shot("08-scenario-running");
await sleep(4200); await shot("09-scenario-results");
await clickText("Apply Plan A", 1500); await shot("10-scenario-applied");

await go("/city", 9000); await shot("11-city-twin");
await clickText("Flood ORR", 4000); await shot("12-city-twin-flood");

await go("/trust", 3000); await clickText("GPS teleport", 2500); await shot("13-trust-teleport");
await clickText("30% blackout", 4000); await shot("14-trust-blackout");

await go("/fidelity", 3000); await shot("15-fidelity-lab");
await go("/network", 5000); await shot("16-network-graph");
await clickText("Fail ", 4500); await shot("17-network-cascade");
await go("/ops", 3000); await shot("18-ops");
await page.evaluate(() => document.querySelector('button[aria-label^="Kill trust-worker"]')?.click());
await sleep(2200); await shot("19-ops-self-heal");

await go("/", 3000);
await page.keyboard.down("Meta"); await page.keyboard.press("j"); await page.keyboard.up("Meta"); await sleep(800);
await clickText("What if a cyclone closes Chennai", 11000); await shot("20-copilot");

await browser.close();
if (errors.length) { console.log("\nPAGE ERRORS:"); for (const e of [...new Set(errors)]) console.log(" -", e); process.exitCode = 1; }
else console.log("no page errors");
