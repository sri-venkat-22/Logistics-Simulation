// Renders every docs/**/*.mmd to SVG + PNG next to its source, using the system Chrome.
import { execFileSync } from "node:child_process";
import { readdirSync, statSync } from "node:fs";
import { join, resolve } from "node:path";

const DOCS = resolve("../docs");
const walk = (d) => readdirSync(d).flatMap((f) => (statSync(join(d, f)).isDirectory() ? walk(join(d, f)) : [join(d, f)]));
const mmdc = resolve("node_modules/.bin/mmdc");
for (const src of walk(DOCS).filter((f) => f.endsWith(".mmd"))) {
  for (const [ext, extra] of [["svg", []], ["png", ["-s", "2"]]]) {
    const out = src.replace(/\.mmd$/, `.${ext}`);
    execFileSync(mmdc, ["-i", src, "-o", out, "-c", "mermaid-config.json", "-p", "puppeteer-config.json", "-b", "white", "-q", ...extra], { stdio: "pipe" });
  }
  console.log("  rendered", src.replace(DOCS + "/", ""));
}
