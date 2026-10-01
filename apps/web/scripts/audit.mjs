// Dependency audit gate for CI: fail on high / critical npm advisories that are not in audit-allowlist.json.
import { execFileSync } from "node:child_process";
import { readFileSync } from "node:fs";

const allow = JSON.parse(readFileSync(new URL("../audit-allowlist.json", import.meta.url), "utf8")).advisories;
let out;
try {
  out = execFileSync("npm", ["audit", "--json", ...process.argv.slice(2)], { encoding: "utf8" });
} catch (e) {
  out = e.stdout; // npm audit exits non-zero when it finds anything
}
const report = JSON.parse(out);
const bad = [];
const accepted = [];
for (const [name, v] of Object.entries(report.vulnerabilities ?? {})) {
  if (!["high", "critical"].includes(v.severity)) continue;
  const ids = v.via.filter((x) => typeof x === "object").map((x) => x.url?.split("/").pop());
  if (ids.length === 0) continue; // only transitively affected: reported under the package that carries the advisory
  const open = ids.filter((id) => !allow[id]);
  (open.length ? bad : accepted).push(`${name} (${v.severity}) ${ids.join(", ")}`);
}
for (const a of accepted) console.log(`accepted: ${a}`);
if (bad.length) {
  console.error(`unreviewed high/critical advisories:\n  ${bad.join("\n  ")}`);
  process.exit(1);
}
console.log(`npm audit gate: ok (${accepted.length} accepted, see audit-allowlist.json)`);
