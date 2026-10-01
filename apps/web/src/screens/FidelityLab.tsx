import { useMemo } from "react";
import { Download, RefreshCcw } from "lucide-react";
import { Chart } from "../components/Chart";
import { Badge, Button, Eyebrow, Glass, PanelHeader, Ticker } from "../components/ui";
import { fidelity, scenario } from "../lib/data";
import { C, axis, chartBase, sevColor } from "../lib/theme";
import { useLive } from "../lib/live";
import FidelityLabLive from "./FidelityLabLive";

function downloadReport() {
  const rows = fidelity.metrics.map((m) => `<tr><td>${m.label}</td><td>${m.value}${m.unit}</td><td>${m.target}</td></tr>`).join("");
  const cm = fidelity.confusion;
  const cmRows = cm.matrix.map((r, i) => `<tr><th>${cm.labels[i]}</th>${r.map((v) => `<td>${v}</td>`).join("")}</tr>`).join("");
  const html = `<!doctype html><meta charset="utf-8"><title>AEGIS evaluation report</title>
<style>body{font:14px system-ui;margin:40px;color:#111}table{border-collapse:collapse;margin:12px 0}td,th{border:1px solid #ccc;padding:4px 10px;text-align:right}th{text-align:left}.warn{background:#fff4d6;padding:8px 12px;border-radius:6px}</style>
<h1>AEGIS Twin — evaluation report</h1><p class="warn"><b>Level-1 prototype:</b> all numbers are seeded mock data (scripts/gen_mock.py, seed 42). Real figures come from GET /eval/report in Phase 9.</p>
<h2>Headline metrics</h2><table><tr><th>Metric</th><th>Value</th><th>Target</th></tr>${rows}</table>
<h2>Attack-detection confusion matrix</h2><table><tr><th></th>${cm.labels.map((l) => `<th>${l}</th>`).join("")}</tr>${cmRows}</table>
<h2>Decision value (index, no action = 100)</h2><table><tr><th>Policy</th><th>Lost sales</th><th>Cost</th><th>CO₂</th></tr>${fidelity.decision_value.map((d) => `<tr><th>${d.policy}</th><td>${d.lost_sales}</td><td>${d.cost}</td><td>${d.co2}</td></tr>`).join("")}</table>
<h2>Reproducibility</h2><p>Monte Carlo seeds 1000–${1000 + scenario.reps - 1} · engine sim.macro 0.1.0 · generated ${new Date().toISOString()}</p>`;
  const a = document.createElement("a");
  a.href = URL.createObjectURL(new Blob([html], { type: "text/html" }));
  a.download = "aegis-eval-report.html";
  a.click();
  URL.revokeObjectURL(a.href);
}

function FidelityLabMock() {
  const eta = fidelity.eta;
  const etaOption = useMemo(() => ({
    ...chartBase,
    grid: { left: 40, right: 16, top: 30, bottom: 26 },
    legend: { top: 0, right: 8, itemWidth: 14, itemHeight: 3, textStyle: { color: C.ink2, fontSize: 11 }, data: ["Actual", "Predicted P50", "P10–P90"] },
    xAxis: { type: "category", data: eta.t.map((x) => `${x}h`), ...axis, axisLabel: { ...axis.axisLabel, interval: 11 } },
    yAxis: { type: "value", ...axis, name: "ETA h", nameTextStyle: { color: C.ink3, fontSize: 10 }, scale: true },
    series: [
      { name: "lo", type: "line", data: eta.p10, stack: "band", symbol: "none", lineStyle: { opacity: 0 }, tooltip: { show: false } },
      { name: "P10–P90", type: "line", data: eta.p90.map((v, i) => +(v - eta.p10[i]).toFixed(2)), stack: "band", symbol: "none", lineStyle: { opacity: 0 }, areaStyle: { color: "rgba(34,211,238,0.14)" }, itemStyle: { color: "rgba(34,211,238,0.4)" }, tooltip: { show: false } },
      { name: "Predicted P50", type: "line", data: eta.pred, symbol: "none", lineStyle: { color: C.ok, width: 1.6 }, itemStyle: { color: C.ok } },
      { name: "Actual", type: "line", data: eta.actual, symbol: "none", lineStyle: { color: C.ink, width: 1.6 }, itemStyle: { color: C.ink } },
    ],
  }), [eta]);

  const relOption = useMemo(() => ({
    ...chartBase,
    grid: { left: 40, right: 16, top: 16, bottom: 34 },
    tooltip: { ...chartBase.tooltip, trigger: "item" },
    xAxis: { type: "value", min: 0, max: 1, name: "nominal coverage", nameLocation: "middle", nameGap: 22, nameTextStyle: { color: C.ink3, fontSize: 10 }, ...axis },
    yAxis: { type: "value", min: 0, max: 1, name: "observed", nameTextStyle: { color: C.ink3, fontSize: 10 }, ...axis },
    series: [
      { type: "line", data: [[0, 0], [1, 1]], symbol: "none", lineStyle: { color: C.ink3, type: "dashed", width: 1 }, silent: true },
      { type: "line", data: fidelity.reliability.nominal.map((n, i) => [n, fidelity.reliability.observed[i]]), symbolSize: 7, lineStyle: { color: C.ok, width: 2 }, itemStyle: { color: C.ok } },
    ],
  }), []);

  const cm = fidelity.confusion;
  const cmOption = useMemo(() => {
    const data: number[][] = [];
    cm.matrix.forEach((row, i) => {
      const tot = row.reduce((a, b) => a + b, 0);
      row.forEach((v, j) => data.push([j, i, +(v / tot).toFixed(3), v]));
    });
    return {
      ...chartBase,
      grid: { left: 78, right: 10, top: 8, bottom: 52 },
      tooltip: { trigger: "item", backgroundColor: chartBase.tooltip.backgroundColor, borderColor: chartBase.tooltip.borderColor, textStyle: chartBase.tooltip.textStyle,
        formatter: (p: { data: number[] }) => `true <b>${cm.labels[p.data[1]]}</b> → predicted <b>${cm.labels[p.data[0]]}</b><br/>${p.data[3]} (${(p.data[2] * 100).toFixed(1)}%)` },
      xAxis: { type: "category", data: cm.labels, ...axis, axisLabel: { ...axis.axisLabel, rotate: 30 }, name: "predicted", nameLocation: "middle", nameGap: 40, nameTextStyle: { color: C.ink3, fontSize: 10 }, splitArea: { show: false } },
      yAxis: { type: "category", data: cm.labels, ...axis, inverse: true },
      visualMap: { show: false, min: 0, max: 1, inRange: { color: ["#0f1626", "#155e75", "#22D3EE"] }, dimension: 2 },
      series: [{ type: "heatmap", data, label: { show: true, formatter: (p: { data: number[] }) => p.data[3], color: C.ink, fontSize: 10, fontFamily: "JetBrains Mono Variable" }, itemStyle: { borderColor: "#070B14", borderWidth: 2 } }],
    };
  }, [cm]);

  const dvOption = useMemo(() => ({
    ...chartBase,
    grid: { left: 40, right: 10, top: 30, bottom: 24 },
    legend: { top: 0, right: 4, itemWidth: 10, itemHeight: 8, textStyle: { color: C.ink2, fontSize: 11 } },
    xAxis: { type: "category", data: fidelity.decision_value.map((d) => d.policy), ...axis },
    yAxis: { type: "value", min: 0, max: 110, ...axis, name: "index", nameTextStyle: { color: C.ink3, fontSize: 10 } },
    series: (["lost_sales", "cost", "co2"] as const).map((k, i) => ({
      name: ["Lost sales", "Cost", "CO₂"][i], type: "bar", barGap: "12%",
      color: [C.ai, "#8b73e0", "#6f5cc0"][i], // legend swatch matches the AEGIS bars
      data: fidelity.decision_value.map((d) => ({ value: d[k], itemStyle: { color: d.policy === "AEGIS" ? [C.ai, "#8b73e0", "#6f5cc0"][i] : ["#5D6A82", "#46526a", "#343e52"][i] } })),
      itemStyle: { borderRadius: [3, 3, 0, 0] },
      label: { show: k === "lost_sales", position: "top", color: C.ink2, fontSize: 10, fontFamily: "JetBrains Mono Variable" },
    })),
  }), []);

  const wOption = useMemo(() => ({
    ...chartBase,
    grid: { left: 150, right: 40, top: 6, bottom: 24 },
    tooltip: { ...chartBase.tooltip, trigger: "item" },
    xAxis: { type: "value", ...axis, name: "W₁ min", nameTextStyle: { color: C.ink3, fontSize: 10 } },
    yAxis: { type: "category", data: fidelity.wasserstein.map((w) => w.corridor), ...axis, axisLabel: { ...axis.axisLabel, fontFamily: "Geist Variable", fontSize: 11, color: C.ink2 } },
    series: [{ type: "bar", data: fidelity.wasserstein.map((w) => ({ value: w.w1_min, itemStyle: { color: w.w1_min > 7 ? C.warn : C.ok } })), barWidth: 10, itemStyle: { borderRadius: 3 },
      label: { show: true, position: "right", color: C.ink2, fontSize: 10, fontFamily: "JetBrains Mono Variable" } }],
  }), []);

  return (
    <div className="absolute inset-0 pt-14 px-4 pb-4 overflow-y-auto scroll-thin">
      <div className="grid grid-cols-4 gap-3">
        {fidelity.metrics.map((m) => (
          <Glass key={m.id} className="px-4 py-3">
            <div className="flex justify-between items-center"><Eyebrow>{m.label}</Eyebrow><span className="num text-[10.5px] text-ink-3">target {m.target}</span></div>
            <div className="mt-1.5 text-[28px] font-semibold leading-none" style={{ color: m.state === "ai" ? sevColor.ai : C.ink }}>
              <Ticker value={m.value} decimals={m.unit === "" ? 2 : 1} /><span className="text-[14px] text-ink-3 ml-1">{m.unit}</span>
            </div>
          </Glass>
        ))}
      </div>

      <div className="mt-3 grid grid-cols-[1.6fr_1fr_1fr] gap-3">
        <Glass><PanelHeader title="ETA · predicted vs actual" sub="Shadow-mode predictions, active shipments, last 72 h" right={<Badge sev="ok">MAPE 8.7%</Badge>} /><Chart option={etaOption} height={250} /></Glass>
        <Glass><PanelHeader title="Reliability diagram" sub="Are P-intervals calibrated? (diagonal = perfect)" /><Chart option={relOption} height={250} /></Glass>
        <Glass><PanelHeader title="Attack detection" sub="Confusion matrix · ≥ 500 labelled attacks + clean" right={<Badge sev="ok">F1 0.94</Badge>} /><Chart option={cmOption} height={250} /></Glass>
      </div>

      <div className="mt-3 grid grid-cols-[1.2fr_1fr_0.9fr] gap-3">
        <Glass><PanelHeader title="Decision value · counterfactual" sub="50 disruptions × 3 policies · common random numbers · index (no action = 100)" /><Chart option={dvOption} height={220} /></Glass>
        <Glass><PanelHeader title="Sim vs observed travel time" sub="Wasserstein-1 distance per SUMO corridor" /><Chart option={wOption} height={220} /></Glass>
        <Glass className="flex flex-col">
          <PanelHeader title="Drift monitor" right={<Badge sev="good"><RefreshCcw size={10} /> self-healed</Badge>} />
          <div className="px-4 text-[12.5px] leading-relaxed text-ink-2 flex-1">{fidelity.drift.detail}</div>
          <div className="px-4 mt-3 grid grid-cols-3 gap-2 text-center">
            {[["Before", "14.2%", C.warn], ["Posterior μ", "+6.1%", C.ai], ["After", "8.9%", C.good]].map(([k, v, c]) => (
              <div key={k} className="rounded-lg bg-black/20 border border-line py-2"><div className="eyebrow !text-[9px]">{k}</div><div className="num text-[14px]" style={{ color: c }}>{v}</div></div>
            ))}
          </div>
          <div className="p-4 pt-3">
            <Button variant="solid" className="w-full" onClick={downloadReport}><Download size={14} /> Download evaluation report</Button>
            <div className="mt-2 text-[10.5px] text-ink-3 text-center">Every metric, chart, seed and engine version · <span className="num">GET /eval/report</span></div>
          </div>
        </Glass>
      </div>
    </div>
  );
}

/** Measured model quality when the API is reachable; the Level-1 prototype otherwise. */
export default function FidelityLab() {
  const apiUp = useLive((s) => s.status === "live" || !!s.network);
  return apiUp ? <FidelityLabLive /> : <FidelityLabMock />;
}
