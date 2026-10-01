import { useEffect, useMemo, useRef, useState } from "react";
import { useNavigate } from "react-router";
import { AnimatePresence, motion } from "motion/react";
import { toast } from "sonner";
import clsx from "clsx";
import type { MapRef, ViewState } from "react-map-gl/maplibre";
import { Anchor, Tornado, Waves, TrendingUp, Factory, Hand, WifiOff, Play, RotateCcw, Sparkles, Check, GripVertical, type LucideIcon } from "lucide-react";
import { DeckMap, useAnimationClock } from "../components/DeckMap";
import { Chart } from "../components/Chart";
import { Badge, Button, Glass, ProgressRing, Ticker } from "../components/ui";
import { network, nodeById, scenario, type Plan } from "../lib/data";
import { cyclonePolygonLayer, networkLayers } from "../lib/networkLayers";
import { useAegis } from "../lib/store";
import { useLive } from "../lib/live";
import ScenarioLabLive from "./ScenarioLabLive";
import { C, axis, chartBase } from "../lib/theme";

const ICONS: Record<string, LucideIcon> = { anchor: Anchor, tornado: Tornado, waves: Waves, "trending-up": TrendingUp, factory: Factory, hand: Hand, "wifi-off": WifiOff };
const DURATION_S = 3.4; // wall-clock length of the mocked 500-run Monte Carlo

function nearestNode(lon: number, lat: number) {
  let best = network.nodes[0], bd = Infinity;
  for (const n of network.nodes) {
    if (n.type === "zone") continue;
    const d = (n.lon - lon) ** 2 + (n.lat - lat) ** 2;
    if (d < bd) { bd = d; best = n; }
  }
  return best;
}

function DisruptionCards() {
  const { scenario: sc, setScenario } = useAegis();
  return (
    <Glass className="w-[212px] shrink-0 flex flex-col overflow-hidden">
      <div className="px-4 pt-3.5 pb-2">
        <div className="text-[13px] font-semibold">Disruptions</div>
        <div className="text-[11.5px] text-ink-3 mt-0.5">Drag onto the map, or click</div>
      </div>
      <div className="flex-1 overflow-y-auto scroll-thin px-2 pb-2 space-y-1">
        {scenario.templates.map((tpl) => {
          const Icon = ICONS[tpl.icon] ?? Tornado;
          const active = sc.type === tpl.type && sc.status !== "idle";
          return (
            <div
              key={tpl.type}
              draggable
              onDragStart={(e) => { e.dataTransfer.setData("text/aegis-disruption", tpl.type); e.dataTransfer.effectAllowed = "copy"; }}
              onClick={() => setScenario({ status: "placed", type: tpl.type, target: tpl.target, progress: 0 })}
              className={clsx(
                "group flex items-center gap-3 rounded-xl px-2.5 py-2.5 cursor-grab active:cursor-grabbing border transition",
                active ? "border-bad/50 bg-bad/8" : "border-transparent hover:border-line-strong hover:bg-white/4",
              )}
            >
              <span className={clsx("w-8 h-8 rounded-lg grid place-items-center shrink-0", active ? "bg-bad/20 text-bad" : "bg-white/5 text-ink-2")}><Icon size={16} /></span>
              <span className="min-w-0 flex-1">
                <span className="block text-[12.5px] font-medium text-ink">{tpl.label}</span>
                <span className="num block text-[10.5px] text-ink-3 truncate">{tpl.duration_h} h · sev {tpl.severity}</span>
              </span>
              <GripVertical size={14} className="text-ink-3 opacity-0 group-hover:opacity-100" />
            </div>
          );
        })}
      </div>
      <div className="p-3 border-t border-line text-[11px] text-ink-3 leading-snug">
        Scenario DSL: <span className="num text-ink-2">type · target · start · duration_h · severity</span>
      </div>
    </Glass>
  );
}

function RunCard() {
  const { scenario: sc, setScenario, resetScenario } = useAegis();
  const tpl = scenario.templates.find((x) => x.type === sc.type);
  if (!tpl || sc.status === "idle") return null;
  const target = nodeById.get(sc.target ?? "")?.name ?? sc.target;
  return (
    <motion.div initial={{ opacity: 0, y: -8 }} animate={{ opacity: 1, y: 0 }} className="absolute left-4 top-4 z-10 glass !bg-[#0c1322]/92 p-3.5 w-[300px]">
      <div className="flex items-center gap-2"><Badge sev="bad">{tpl.label}</Badge><span className="num text-[11px] text-ink-3 ml-auto">sc_7f3a</span></div>
      <div className="mt-2 text-[13px] text-ink font-medium truncate">{target}</div>
      <div className="num mt-1 text-[11px] text-ink-3">start +6 h · {tpl.duration_h} h · severity {tpl.severity}</div>
      {sc.status === "running" ? (
        <div className="mt-3 flex items-center gap-3">
          <ProgressRing value={sc.progress} size={56} stroke={5}>
            <span className="num text-[11px] text-ink">{Math.round(sc.progress * 100)}%</span>
          </ProgressRing>
          <div>
            <div className="num text-[15px] text-ink"><Ticker value={Math.round(sc.progress * scenario.reps)} duration={100} /> / {scenario.reps}</div>
            <div className="text-[11px] text-ink-3">Monte Carlo replications · 8 workers</div>
          </div>
        </div>
      ) : (
        <div className="mt-3 flex gap-2">
          <Button variant="primary" className="flex-1" onClick={() => setScenario({ status: "running", progress: 0 })} disabled={sc.status === "done"}>
            <Play size={13} /> {sc.status === "done" ? `${scenario.reps} runs done` : `Run ${scenario.reps} simulations`}
          </Button>
          <Button onClick={resetScenario} aria-label="Reset"><RotateCcw size={13} /></Button>
        </div>
      )}
    </motion.div>
  );
}

function FanChart({ plan }: { plan: Plan }) {
  const band = plan.id === "B" ? scenario.air : scenario.mitigated;
  const x = scenario.t_days;
  const pair = (a: number[]) => a.map((v, i) => [x[i], v]);
  const diff = (hi: number[], lo: number[]) => hi.map((v, i) => [x[i], v - lo[i]]);
  const option = useMemo(() => ({
    ...chartBase,
    grid: { left: 44, right: 14, top: 30, bottom: 26 },
    legend: { top: 0, right: 8, itemWidth: 14, itemHeight: 3, textStyle: { color: C.ink2, fontSize: 11 }, data: ["Baseline P50", `Plan ${plan.id} P50`] },
    xAxis: { type: "value", min: 0, max: 20, ...axis, axisLabel: { ...axis.axisLabel, formatter: "{value} d" } },
    yAxis: { type: "value", ...axis, name: "units", nameTextStyle: { color: C.ink3, fontSize: 10 } },
    tooltip: { ...chartBase.tooltip, valueFormatter: (v: number) => Math.round(v).toLocaleString("en-IN") },
    series: [
      { name: "b-lo", type: "line", data: pair(scenario.baseline.p10), stack: "b", symbol: "none", lineStyle: { opacity: 0 }, tooltip: { show: false } },
      { name: "Baseline P10–P90", type: "line", data: diff(scenario.baseline.p90, scenario.baseline.p10), stack: "b", symbol: "none", lineStyle: { opacity: 0 }, areaStyle: { color: "rgba(239,68,68,0.16)" }, tooltip: { show: false } },
      { name: "m-lo", type: "line", data: pair(band.p10), stack: "m", symbol: "none", lineStyle: { opacity: 0 }, tooltip: { show: false } },
      { name: `Plan ${plan.id} P10–P90`, type: "line", data: diff(band.p90, band.p10), stack: "m", symbol: "none", lineStyle: { opacity: 0 }, areaStyle: { color: "rgba(167,139,250,0.18)" }, tooltip: { show: false } },
      {
        name: "Baseline P50", type: "line", data: pair(scenario.baseline.p50), symbol: "none", lineStyle: { color: C.bad, width: 2 }, itemStyle: { color: C.bad },
        markLine: {
          symbol: "none", silent: true,
          label: { color: C.ink2, fontSize: 10, fontFamily: "JetBrains Mono Variable" },
          data: [
            { yAxis: scenario.safety_stock, lineStyle: { color: C.warn, type: "dashed", opacity: 0.7 }, label: { formatter: "safety stock", position: "insideEndTop", color: C.warn } },
            { xAxis: scenario.tts_d, lineStyle: { color: C.bad, type: "solid", opacity: 0.8 }, label: { formatter: `TTS ${scenario.tts_d} d`, position: "insideEndTop", color: C.bad } },
            { xAxis: scenario.ttr_d, lineStyle: { color: C.ink3, type: "dashed" }, label: { formatter: `TTR ${scenario.ttr_d} d`, position: "insideEndTop" } },
          ],
        },
      },
      { name: `Plan ${plan.id} P50`, type: "line", data: pair(band.p50), symbol: "none", lineStyle: { color: C.ai, width: 2 }, itemStyle: { color: C.ai } },
    ],
  }), [plan.id]); // eslint-disable-line react-hooks/exhaustive-deps
  return <Chart option={option} height="100%" />;
}

function Pareto({ weights, selected, onSelect }: { weights: number[]; selected: string; onSelect: (id: string) => void }) {
  const option = useMemo(() => {
    const cands = scenario.candidates;
    const sorted = [...cands].sort((a, b) => a.cost_lakh - b.cost_lakh);
    const front: number[][] = [];
    let best = -Infinity;
    for (const c of sorted) if (c.service > best) { best = c.service; front.push([c.cost_lakh, c.service]); }
    return {
      ...chartBase,
      grid: { left: 40, right: 14, top: 18, bottom: 30 },
      tooltip: { trigger: "item", backgroundColor: chartBase.tooltip.backgroundColor, borderColor: chartBase.tooltip.borderColor, textStyle: chartBase.tooltip.textStyle,
        formatter: (p: { data: { name?: string; value: number[] } }) => `${p.data.name ?? "Candidate"}<br/>₹${p.data.value[0]} L · ${p.data.value[1]}% service · ${p.data.value[2]} t CO₂` },
      xAxis: { type: "value", name: "cost ₹ L", nameLocation: "middle", nameGap: 20, nameTextStyle: { color: C.ink3, fontSize: 10 }, ...axis },
      yAxis: { type: "value", min: 75, max: 100, name: "service %", nameTextStyle: { color: C.ink3, fontSize: 10 }, ...axis },
      series: [
        { type: "scatter", data: cands.map((c) => ({ value: [c.cost_lakh, c.service, c.co2_t] })), symbolSize: (v: number[]) => 4 + v[2] / 10, itemStyle: { color: "rgba(154,167,189,0.35)" } },
        { type: "line", data: front, symbol: "none", lineStyle: { color: "rgba(154,167,189,0.5)", type: "dashed", width: 1 }, tooltip: { show: false }, silent: true },
        {
          type: "scatter", zlevel: 2,
          data: scenario.plans.map((p) => ({ name: `Plan ${p.id} — ${p.name}`, id: p.id, value: [p.cost_lakh, p.service, p.co2_t],
            itemStyle: { color: p.id === "A" ? C.ai : p.id === "C" ? C.bad : C.ink, borderColor: p.id === selected ? "#fff" : "transparent", borderWidth: 2 } })),
          symbolSize: 14,
          label: { show: true, formatter: (p: { data: { id: string } }) => p.data.id, color: C.bg, fontWeight: 700, fontSize: 10 },
        },
      ],
    };
  }, [selected, weights]); // eslint-disable-line react-hooks/exhaustive-deps
  return <Chart option={option} height="100%" onEvents={{ click: (e) => { const id = (e as { data?: { id?: string } }).data?.id; if (id) onSelect(id); } }} />;
}

function scorePlan(p: Plan, w: number[]) {
  const svc = (p.service - 80) / 20, cost = p.cost_lakh / 65, co2 = p.co2_t / 60;
  const tot = w[0] + w[1] + w[2] || 1;
  return (w[0] * svc + w[1] * (1 - cost) + w[2] * (1 - co2)) / tot;
}

export default function ScenarioLab() {
  const offline = useLive((s) => s.status === "offline" && !s.network);
  return offline ? <MockScenarioLab /> : <ScenarioLabLive />;
}

/** Level-1 prototype on seeded mock data (used when the API is not reachable). */
function MockScenarioLab() {
  const t = useAnimationClock();
  const navigate = useNavigate();
  const { scenario: sc, setScenario, appliedPlan, applyPlan } = useAegis();
  const mapRef = useRef<MapRef>(null);
  const [view, setView] = useState<Partial<ViewState>>({ longitude: 80.2, latitude: 16.4, zoom: 4.6, pitch: 30, bearing: 0 });
  const [selected, setSelected] = useState("A");
  const [weights, setWeights] = useState([0.5, 0.3, 0.2]);
  const [dragOver, setDragOver] = useState(false);

  useEffect(() => {
    if (sc.status !== "running") return;
    const t0 = performance.now();
    const id = window.setInterval(() => {
      const p = Math.min(1, (performance.now() - t0) / 1000 / DURATION_S);
      setScenario({ progress: p });
      if (p >= 1) {
        window.clearInterval(id);
        setScenario({ status: "done" });
        toast.success(`${scenario.reps} runs complete`, { description: `Hyderabad-Shamshabad stocks out in ${scenario.tts_d} d (TTS) · Chennai TTR ${scenario.ttr_d} d` });
      }
    }, 60);
    return () => window.clearInterval(id);
  }, [sc.status, setScenario]);

  const placed = sc.status !== "idle";
  const done = sc.status === "done";
  const plan = scenario.plans.find((p) => p.id === selected)!;
  const ranked = [...scenario.plans].sort((a, b) => scorePlan(b, weights) - scorePlan(a, weights));
  const target = nodeById.get(sc.target ?? "PORT_CHENNAI") ?? nodeById.get("PORT_CHENNAI")!;
  const cyc = sc.type === "cyclone" || sc.type === "port_closure";

  const disruptionLayers = placed && cyc ? [cyclonePolygonLayer(t)] : [];
  const baseLayers = [...disruptionLayers, ...networkLayers({ t, scenarioActive: placed, idPrefix: "b-", showLabels: true })];
  const mitLayers = [...disruptionLayers.map((l) => l.clone({ id: "cyclone-m" })), ...networkLayers({ t, scenarioActive: placed, mitigated: true, idPrefix: "m-", showLabels: true })];

  const onDrop = (e: React.DragEvent) => {
    e.preventDefault();
    setDragOver(false);
    const type = e.dataTransfer.getData("text/aegis-disruption");
    if (!type) return;
    const rect = (e.currentTarget as HTMLElement).getBoundingClientRect();
    const ll = mapRef.current?.unproject([e.clientX - rect.left, e.clientY - rect.top]);
    const node = ll ? nearestNode(ll.lng, ll.lat) : target;
    setScenario({ status: "placed", type, target: node.id, progress: 0 });
    toast(`${scenario.templates.find((x) => x.type === type)?.label} placed on ${node.name}`, { description: "Press Run to simulate" });
  };

  return (
    <div className="absolute inset-0 pt-14 px-4 pb-4 flex gap-3">
      <DisruptionCards />
      <div className="flex-1 min-w-0 flex flex-col gap-3">
        <div
          className={clsx("relative flex-1 min-h-0 rounded-[14px] overflow-hidden border transition", dragOver ? "border-ok/60" : "border-line")}
          onDragOver={(e) => { e.preventDefault(); setDragOver(true); }}
          onDragLeave={() => setDragOver(false)}
          onDrop={onDrop}
        >
          <div className={clsx("absolute inset-0 grid transition-all", done ? "grid-cols-2 gap-px bg-line" : "grid-cols-1")}>
            <div className="relative">
              <DeckMap ref={mapRef} className="absolute inset-0" viewState={view} onMove={setView} layers={baseLayers} />
              {done && <div className="absolute left-3 bottom-3 glass px-3 py-1.5 text-[11.5px] flex items-center gap-2"><span className="w-2 h-2 rounded-full bg-bad" /> BASELINE · no action</div>}
            </div>
            {done && (
              <div className="relative">
                <DeckMap className="absolute inset-0" viewState={view} onMove={setView} layers={mitLayers} />
                <div className="absolute left-3 bottom-3 glass px-3 py-1.5 text-[11.5px] flex items-center gap-2"><span className="w-2 h-2 rounded-full bg-ai" /> MITIGATED · Plan A (reroute via Vizag)</div>
              </div>
            )}
          </div>
          {!placed && (
            <div className="absolute inset-0 grid place-items-center pointer-events-none">
              <div className="glass px-5 py-4 text-center max-w-[340px]">
                <Tornado size={22} className="mx-auto text-warn" />
                <div className="mt-2 text-[14px] font-medium">Drop a disruption on the network</div>
                <div className="mt-1 text-[12px] text-ink-3">Or press <span className="num text-ink-2">D</span> for the demo: a 5-day cyclone over Chennai port.</div>
              </div>
            </div>
          )}
          <RunCard />
        </div>

        <div className="h-[330px] shrink-0 grid grid-cols-[1.25fr_1fr_1.15fr] gap-3">
          {!done ? (
            <Glass className="col-span-3 grid place-items-center">
              <div className="text-center">
                <div className="grid grid-cols-3 gap-3 w-[560px] mb-5 opacity-60">{[0, 1, 2].map((i) => <div key={i} className="skeleton h-24" />)}</div>
                <div className="text-[13px] text-ink-2">{sc.status === "running" ? `Running ${scenario.reps} Monte Carlo replications with common random numbers…` : "Results appear here: fan chart of stock (P10–P90), Pareto front of plans, and ranked recommendations."}</div>
              </div>
            </Glass>
          ) : (
            <>
              <Glass className="flex flex-col min-w-0">
                <div className="px-4 pt-3 flex items-center justify-between">
                  <div>
                    <div className="text-[13px] font-semibold">Vaccine stock · Hyderabad-Shamshabad</div>
                    <div className="text-[11px] text-ink-3">{scenario.reps} runs · P10–P90 bands</div>
                  </div>
                  <Badge sev="bad">TTS {scenario.tts_d} d &lt; TTR {scenario.ttr_d} d</Badge>
                </div>
                <div className="flex-1 min-h-0"><FanChart plan={plan} /></div>
              </Glass>
              <Glass className="flex flex-col min-w-0">
                <div className="px-4 pt-3">
                  <div className="text-[13px] font-semibold">Pareto front</div>
                  <div className="text-[11px] text-ink-3">{scenario.candidates.length} candidate plans · size = CO₂</div>
                </div>
                <div className="flex-1 min-h-0"><Pareto weights={weights} selected={selected} onSelect={setSelected} /></div>
                <div className="px-4 pb-3 grid grid-cols-3 gap-3">
                  {["Service", "Cost", "CO₂"].map((k, i) => (
                    <label key={k} className="text-[10.5px] text-ink-3">
                      <span className="flex justify-between"><span>{k}</span><span className="num text-ink-2">{weights[i].toFixed(1)}</span></span>
                      <input type="range" min={0} max={1} step={0.1} value={weights[i]} className="w-full accent-[#A78BFA]"
                        onChange={(e) => setWeights(weights.map((w, j) => (j === i ? Number(e.target.value) : w)))} />
                    </label>
                  ))}
                </div>
              </Glass>
              <Glass className="flex flex-col min-w-0 overflow-hidden">
                <div className="px-4 pt-3 pb-1 flex items-center justify-between">
                  <div className="text-[13px] font-semibold">Recommended plans</div>
                  <span className="text-[11px] text-ink-3">ranked by weighted score</span>
                </div>
                <div className="flex-1 overflow-y-auto scroll-thin px-2 pb-2 space-y-1.5">
                  <AnimatePresence>
                    {ranked.map((p, idx) => {
                      const base = scenario.plans.find((x) => x.id === "C")!;
                      const isApplied = appliedPlan === p.id;
                      return (
                        <motion.div layout key={p.id} onClick={() => setSelected(p.id)}
                          className={clsx("rounded-xl border p-3 cursor-pointer transition", selected === p.id ? "border-ai/50 bg-ai/6" : "border-line hover:border-line-strong")}>
                          <div className="flex items-center gap-2">
                            <span className={clsx("num w-6 h-6 rounded-md grid place-items-center text-[11px] font-bold", idx === 0 ? "bg-ai text-bg" : "bg-white/8 text-ink-2")}>{p.id}</span>
                            <span className="text-[12.5px] font-medium text-ink truncate flex-1">{p.name}</span>
                            {idx === 0 && <Badge sev="ai"><Sparkles size={10} /> Best</Badge>}
                          </div>
                          <div className="mt-2 grid grid-cols-4 gap-1.5 text-center">
                            {[
                              ["Service", `${p.service}%`, p.id === "C" ? "" : `+${(p.service - base.service).toFixed(1)}`],
                              ["Cost", `₹${p.cost_lakh}L`, ""],
                              ["CO₂", `${p.co2_t}t`, ""],
                              ["P(stockout)", `${Math.round(p.stockout_p * 100)}%`, ""],
                            ].map(([k, v, d]) => (
                              <div key={k} className="rounded-md bg-black/20 py-1">
                                <div className="text-[9px] uppercase tracking-wider text-ink-3">{k}</div>
                                <div className="num text-[12px] text-ink">{v}</div>
                                {d && <div className="num text-[9.5px] text-good">{d} pp</div>}
                              </div>
                            ))}
                          </div>
                          {selected === p.id && (
                            <div className="mt-2.5">
                              <p className="text-[11.5px] leading-snug text-ink-2">{p.explain}</p>
                              <ul className="mt-2 space-y-1">{p.actions.map((a) => <li key={a} className="num text-[10.5px] text-ink-3">→ {a}</li>)}</ul>
                              {p.id !== "C" && (
                                <div className="mt-2.5 flex gap-2">
                                  <Button variant="ai" className="flex-1" disabled={isApplied} onClick={(e) => {
                                    e.stopPropagation();
                                    applyPlan(p.id);
                                    toast.success(`Plan ${p.id} applied`, { description: "Pushed to live twin · audit_log #A-2291 · trucks rerouting", action: { label: "View map", onClick: () => navigate("/") } });
                                  }}>{isApplied ? <><Check size={13} /> Applied</> : `Apply Plan ${p.id}`}</Button>
                                  {isApplied && <Button onClick={(e) => { e.stopPropagation(); navigate("/city"); }}>City Twin →</Button>}
                                </div>
                              )}
                            </div>
                          )}
                        </motion.div>
                      );
                    })}
                  </AnimatePresence>
                </div>
              </Glass>
            </>
          )}
        </div>
      </div>
    </div>
  );
}
