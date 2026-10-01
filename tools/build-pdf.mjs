// Builds print-ready PDFs from the Markdown docs (SRS, roadmap) and renders the research slide
// to PNG + PDF. Uses the system Chrome via puppeteer-core.
// Usage: node build-pdf.mjs
import puppeteer from "puppeteer-core";
import { marked } from "marked";
import { readFileSync, writeFileSync } from "node:fs";
import { dirname, resolve } from "node:path";
import { pathToFileURL } from "node:url";

const CHROME = process.env.CHROME_PATH ?? "/Applications/Google Chrome.app/Contents/MacOS/Google Chrome";
const DOCS = resolve("../docs");

const css = `
  @page { size: A4; margin: 16mm 15mm 18mm; }
  * { box-sizing: border-box; }
  body { font: 10.2pt/1.5 "Helvetica Neue", Helvetica, Arial, sans-serif; color: #0f172a; }
  h1 { font-size: 24pt; margin: 0 0 4pt; color: #0b2545; letter-spacing: -0.01em; }
  h2 { font-size: 15pt; margin: 20pt 0 8pt; padding-bottom: 4pt; border-bottom: 2px solid #1168bd; color: #0b2545; break-after: avoid; }
  h3 { font-size: 12pt; margin: 14pt 0 6pt; color: #1e3a5f; break-after: avoid; }
  h4 { font-size: 10.5pt; margin: 12pt 0 5pt; color: #1e3a5f; break-after: avoid; }
  p, li { orphans: 3; widows: 3; }
  blockquote { margin: 8pt 0; padding: 6pt 12pt; border-left: 3px solid #f59e0b; background: #fffbeb; color: #78350f; }
  table { width: 100%; border-collapse: collapse; margin: 6pt 0 10pt; font-size: 8.6pt; break-inside: auto; }
  tr { break-inside: avoid; }
  th, td { border: 1px solid #cbd5e1; padding: 3.5pt 5pt; vertical-align: top; text-align: left; }
  th { background: #eef4fb; color: #0b2545; }
  code { font: 8.6pt "SF Mono", Menlo, monospace; background: #f1f5f9; padding: 0 2pt; border-radius: 2pt; }
  pre { background: #0f172a; color: #e2e8f0; padding: 8pt 10pt; border-radius: 4pt; font-size: 8.4pt; white-space: pre-wrap; }
  pre code { background: none; color: inherit; }
  img { max-width: 100%; display: block; margin: 6pt auto 2pt; border: 1px solid #e2e8f0; border-radius: 3pt; break-inside: avoid; }
  img[src*="dfd-level1"] { max-height: 200mm; width: auto; }
  p:has(> img) { text-align: center; font-size: 8.6pt; color: #475569; break-inside: avoid; margin: 8pt 0 12pt; }
  @page land { size: A4 landscape; margin: 14mm 14mm 16mm; }
  .land { page: land; break-before: page; break-after: page; }
  .land img { width: 100%; max-height: 142mm; object-fit: contain; }
  hr { display: none; }
  .cover { height: 250mm; display: flex; flex-direction: column; justify-content: center; }
  .cover .eyebrow { font: 600 10pt Menlo, monospace; letter-spacing: .16em; color: #1168bd; text-transform: uppercase; }
  .cover h1 { font-size: 34pt; margin-top: 8pt; }
  .cover .tag { font-size: 14pt; color: #475569; margin-top: 8pt; font-style: italic; }
  .cover table { margin-top: 26pt; font-size: 10pt; width: 70%; }
  .toc { break-after: page; }
  .toc li { margin: 2pt 0; }
`;

const WIDE = /c4-|seq-|erd\.png|dfd-level0|gantt/;

function figureCaptions(html) {
  // Markdown images become <p><img alt="Figure n — …"></p>; append the alt text as a visible caption.
  // Wide diagrams get their own landscape page so their labels stay legible.
  return html.replace(/<p>(<img [^>]*src="([^"]+)"[^>]*alt="([^"]+)"[^>]*>)<\/p>/g, (_, img, src, alt) =>
    WIDE.test(src) ? `<div class="land"><p>${img}${alt}</p></div>` : `<p>${img}${alt}</p>`);
}

async function mdToPdf(browser, mdPath, pdfPath, { cover, title }) {
  let md = readFileSync(mdPath, "utf8").replace(/^---[\s\S]*?---\n/, "");
  const headings = [...md.matchAll(/^## (.+)$/gm)].map((m) => m[1]);
  md = md.trimStart().replace(/^# .+\n/, ""); // title goes on the cover
  const body = figureCaptions(marked.parse(md));
  const toc = `<div class="toc"><h2>Contents</h2><ol>${headings.map((h) => `<li>${h.replace(/^\d+\.\s*/, "")}</li>`).join("")}</ol></div>`;
  const html = `<!doctype html><html><head><meta charset="utf-8"><title>${title}</title><base href="${pathToFileURL(dirname(mdPath))}/"><style>${css}</style></head>
  <body><section class="cover">${cover}</section>${toc}${body}</body></html>`;
  const tmp = resolve(dirname(mdPath), ".print.html");
  writeFileSync(tmp, html);
  const page = await browser.newPage();
  await page.goto(pathToFileURL(tmp).href, { waitUntil: "networkidle0" });
  // Pull each landscape figure's heading (and a short lead paragraph) onto the landscape page,
  // so no portrait page is left holding only a heading.
  await page.evaluate(() => {
    for (const land of document.querySelectorAll(".land")) {
      const moved = [];
      let prev = land.previousElementSibling;
      if (prev?.tagName === "P" && prev.textContent.length < 600 && /^H[234]$/.test(prev.previousElementSibling?.tagName ?? "")) {
        moved.unshift(prev); prev = prev.previousElementSibling;
      }
      while (prev && /^H[234]$/.test(prev.tagName)) { moved.unshift(prev); prev = prev.previousElementSibling; }
      land.prepend(...moved);
    }
  });
  await page.pdf({
    path: pdfPath, format: "A4", printBackground: true, displayHeaderFooter: true,
    headerTemplate: `<div style="font:7pt Helvetica;color:#94a3b8;width:100%;padding:0 15mm;text-align:right">${title}</div>`,
    footerTemplate: `<div style="font:7pt Helvetica;color:#94a3b8;width:100%;padding:0 15mm;display:flex;justify-content:space-between"><span>AEGIS Twin · PNT1 · Level 1</span><span><span class="pageNumber"></span> / <span class="totalPages"></span></span></div>`,
    margin: { top: "16mm", bottom: "18mm", left: "15mm", right: "15mm" },
  });
  await page.close();
  const { unlinkSync } = await import("node:fs");
  unlinkSync(tmp);
  console.log("  wrote", pdfPath.replace(DOCS + "/", "docs/"));
}

const browser = await puppeteer.launch({ executablePath: CHROME, headless: true });

await mdToPdf(browser, `${DOCS}/srs/SRS.md`, `${DOCS}/srs/SRS.pdf`, {
  title: "AEGIS Twin — Software Requirements Specification v1.0",
  cover: `<div class="eyebrow">NRCM Hackathon · PNT1 · Level 1</div><h1>AEGIS Twin</h1>
    <div style="font-size:18pt;color:#1e3a5f">Software Requirements Specification</div>
    <div class="tag">See every shipment. Simulate every shock. Survive every attack.</div>
    <table><tr><th>Standard</th><td>IEEE 830-lite</td></tr><tr><th>Version</th><td>1.0 — Level 1 baseline</td></tr>
    <tr><th>Date</th><td>30 September 2026</td></tr><tr><th>Scope</th><td>FR-1…FR-25 · NFR-1…NFR-12 · architecture · DFD · ERD · prototype · roadmap</td></tr></table>`,
});
await mdToPdf(browser, `${DOCS}/roadmap/ROADMAP.md`, `${DOCS}/roadmap/ROADMAP.pdf`, {
  title: "AEGIS Twin — Tech stack, roadmap and risk register",
  cover: `<div class="eyebrow">NRCM Hackathon · PNT1 · Level 1</div><h1>AEGIS Twin</h1>
    <div style="font-size:18pt;color:#1e3a5f">Tech stack, roadmap &amp; risk register</div>
    <div class="tag">Phases 2–11 · Day 1 = 1 Oct 2026</div>`,
});

// slides → PNG + PDF (16:9): the title slide (live URL) first, then the research slide
const page = await browser.newPage();
await page.setViewport({ width: 1920, height: 1080, deviceScaleFactor: 1 });
for (const slide of ["title-slide", "research-slide"]) {
  await page.goto(pathToFileURL(`${DOCS}/pitch/${slide}.html`).href, { waitUntil: "networkidle0" });
  await page.evaluate(() => document.fonts.ready);
  await page.screenshot({ path: `${DOCS}/pitch/${slide}.png` });
  await page.pdf({ path: `${DOCS}/pitch/${slide}.pdf`, width: "1920px", height: "1080px", printBackground: true, pageRanges: "1" });
  console.log(`  wrote docs/pitch/${slide}.{png,pdf}`);
}
await browser.close();
