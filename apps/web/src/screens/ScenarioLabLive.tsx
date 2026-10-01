/**
 * Scenario Lab v1 on the live API (Phase 5): pick a template -> run N Monte Carlo replications (worker pool,
 * progress over WS /ws/scenarios/{id}) -> fan chart of on-hand stock (P10-P90, scenario vs baseline) for the most
 * exposed DC x SKU, KPI deltas vs the baseline, then optimise -> ranked plans -> Apply (planner token).
 */
import { useEffect, useMemo, useRef, useState } from "react";
import { toast } from "sonner";
import clsx from "clsx";
import { Anchor, Tornado, Waves, TrendingUp, Factory, Hand, WifiOff, Play, Sparkles, Loader2, type LucideIcon } from "lucide-react";
import { DeckMap, useAnimationClock } from "../components/DeckMap";
import { Chart } from "../components/Chart";
import { Badge, Button, Eyebrow, Glass, ProgressRing, Ticker } from "../components/ui";
import { useNavigate } from "react-router";
import { Zap } from "lucide-react";
import { wsUrl, createScenario, getScenario, getTemplates, optimizeScenario, pushDisruption, type ScenarioStatus, type Template } from "../lib/api";
import { useLive, vehicles as liveVehicles } from "../lib/live";
import { liveLayers } from "../lib/liveLayers";
import { useAegis } from "../lib/store";
import { ensureRole } from "../lib/auth";
import { PlansPanel } from "../components/PlansPanel";
import { C, axis, chartBase } from "../lib/theme";
import { PolygonLayer, ScatterplotLayer } from "@deck.gl/layers";

const ICONS: Record<string, LucideIcon> = {
  port_closure: Anchor, cyclone: Tornado, road_flood: Waves, demand_spike: TrendingUp, supplier_failure: Factory, strike: Hand, data_blackout: WifiOff,
};

const KPI_ROWS: { key: string; label: string; fmt: (v: number) => string; good: "up" | "down" }[] = [
  { key: "fill_rate", label: "Fill rate", fmt: (v) => `${(v * 100).toFixed(2)} %`, good: "up" },
  { key: "otif", label: "OTIF", fmt: (v) => `${(v * 100).toFixed(2)} %`, good: "up" },
  { key: "units_backordered", label: "Units backordered", fmt: (v) => Math.round(v).toLocaleString("en-IN"), good: "down" },
  { key: "stockout_episodes", label: "Stock-out episodes", fmt: (v) => v.toFixed(1), good: "down" },
  { key: "cost_total", label: "Total cost", fmt: (v) => `₹${(v / 1e5).toFixed(1)} L`, good: "down" },
  { key: "co2_t", label: "CO₂", fmt: (v) => `${v.toFixed(1)} t`, good: "down" },
];

function useScenarioRun() {
  const [st, setSt] = useState<ScenarioStatus | null>(null);
  const [base, setBase] = useState<ScenarioStatus | null>(null);
  const wsRef = useRef<WebSocket | null>(null);

  const follow = (id: string) => {
    wsRef.current?.close();
    const ws = new WebSocket(wsUrl(`/ws/scenarios/${id}`));
    wsRef.current = ws;
    ws.onmessage = (e) => {
      const ev = JSON.parse(e.data as string) as ScenarioStatus;
      setSt((prev) => ({ ...(prev ?? ev), ...ev, result: prev?.result }));
      if (ev.status === "done") void load(id);
      if (ev.status === "error") toast.error("Scenario failed", { description: ev.error });
    };
  };

  const load = async (id: string) => {
    for (let i = 0; i < 60; i++) {  // the baseline job may still be finishing
      const full = await getScenario(id);
      setSt(full);
      if (full.baseline_id) {
        const b = await getScenario(full.baseline_id);
        setBase(b);
        if (b.status === "done" && full.deltas) return full;
      } else if (full.status === "done") return full;
      await new Promise((r) => setTimeout(r, 500));
    }
    return null;
  };

  const run = async (template: string, n: number, days: number) => {
    setSt(null); setBase(null);
    const s = await createScenario({ template, n, days });
    setSt(s);
    if (s.status === "done") { toast.success("Answered from cache", { description: `spec ${s.spec_hash.slice(0, 12)}… · ${n} runs` }); await load(s.id); }
    else follow(s.id);
  };

  useEffect(() => () => wsRef.current?.close(), []);
  return { st, base, run, load, setSt };
}

function FanChart({ st, base, pair }: { st: ScenarioStatus; base: ScenarioStatus | null; pair: string }) {
  const s = st.result!.series[pair];
  const b = base?.result?.series[pair];
  const option = useMemo(() => {
    const x = s.t_days;
    const pts = (a: number[]) => a.map((v, i) => [x[i], v]);
    const span = (hi: number[], lo: number[]) => hi.map((v, i) => [x[i], v - lo[i]]);
    const series: object[] = [];
    if (b) {
      series.push(
        { name: "b-lo", type: "line", data: pts(b.on_hand.p10), stack: "b", symbol: "none", lineStyle: { opacity: 0 }, tooltip: { show: false } },
        { name: "Baseline P10–P90", type: "line", data: span(b.on_hand.p90, b.on_hand.p10), stack: "b", symbol: "none", lineStyle: { opacity: 0 }, areaStyle: { color: "rgba(34,211,238,0.12)" }, tooltip: { show: false } },
        { name: "Baseline P50", type: "line", data: pts(b.on_hand.p50), symbol: "none", lineStyle: { color: C.ok, width: 1.6, type: "dashed" }, itemStyle: { color: C.ok } },
      );
    }
    series.push(
      { name: "s-lo", type: "line", data: pts(s.on_hand.p10), stack: "s", symbol: "none", lineStyle: { opacity: 0 }, tooltip: { show: false } },
      { name: "Scenario P10–P90", type: "line", data: span(s.on_hand.p90, s.on_hand.p10), stack: "s", symbol: "none", lineStyle: { opacity: 0 }, areaStyle: { color: "rgba(239,68,68,0.18)" }, tooltip: { show: false } },
      { name: "Scenario P50", type: "line", data: pts(s.on_hand.p50), symbol: "none", lineStyle: { color: C.bad, width: 2 }, itemStyle: { color: C.bad } },
    );
    return {
      ...chartBase,
      grid: { left: 52, right: 14, top: 30, bottom: 26 },
      legend: { top: 0, right: 6, itemWidth: 14, itemHeight: 3, textStyle: { color: C.ink2, fontSize: 11 }, data: ["Baseline P50", "Scenario P50"] },
      xAxis: { type: "value", min: 0, max: x[x.length - 1], ...axis, axisLabel: { ...axis.axisLabel, formatter: "{value} d" } },
      yAxis: { type: "value", ...axis, name: "on hand", nameTextStyle: { color: C.ink3, fontSize: 10 } },
      tooltip: { ...chartBase.tooltip, valueFormatter: (v: number) => Math.round(v).toLocaleString("en-IN") },
      series,
    };
  }, [s, b]);
  return <Chart option={option} height="100%" />;
}

export default function ScenarioLabLive() {
  const t = useAnimationClock();
  const { network, inventory, ports, dataVersion, planShipments } = useLive();
  const { layers } = useAegis();
  const [templates, setTemplates] = useState<Template[]>([]);
  const [sel, setSel] = useState<string>("supplier_failure");
  const [n, setN] = useState(200);
  const [days, setDays] = useState(30);
  const [pair, setPair] = useState<string | null>(null);
  const [optimizing, setOptimizing] = useState(false);
  const [pushed, setPushed] = useState<string | null>(null);
  const navigate = useNavigate();
  const { selectNode } = useAegis();
  const { st, base, run, load } = useScenarioRun();

  useEffect(() => { getTemplates().then(setTemplates).catch(() => toast.error("Cannot load scenario templates")); }, []);
  const tpl = templates.find((x) => x.template === sel);
  const running = st && (st.status === "queued" || st.status === "running");
  const res = st?.result;

  // rank DC x SKU pairs by what the disruption adds: stock-out probability over the baseline, then the drop in P50 stock
  const pairs = useMemo(() => {
    if (!res) return [] as [string, number, number][];
    const bs = base?.result;
    return Object.keys(res.series).map((k) => {
      const dp = res.stockout_prob[k] - (bs?.stockout_prob[k] ?? 0);
      const a = res.series[k].on_hand.p50, b = bs?.series[k]?.on_hand.p50;
      const drop = b ? Math.max(...b.map((v, i) => (v - a[i]) / Math.max(v, 1))) : 0;
      return [k, dp, drop] as [string, number, number];
    }).sort((x, y) => y[1] - x[1] || y[2] - x[2] || x[0].localeCompare(y[0]));
  }, [res, base]);
  useEffect(() => { if (pairs.length) setPair(pairs[0][0]); }, [pairs]);

  const optimize = async () => {
    if (!st || !ensureRole("planner")) return;
    setOptimizing(true);
    try {
      await optimizeScenario(st.id, Math.min(100, n));
      for (let i = 0; i < 240; i++) {
        const g = await getScenario(st.id, false);
        if (g.plans?.length) { await load(st.id); break; }
        await new Promise((r) => setTimeout(r, 500));
      }
    } catch (e) { toast.error("Optimise failed", { description: String(e) }); }
    setOptimizing(false);
  };

  const risk = useMemo(() => {
    const out: Record<string, number> = {};
    for (const [k, dp] of pairs.map(([k, dp]) => [k, dp] as [string, number])) {
      const dc = k.split("/")[0];
      out[dc] = Math.max(out[dc] ?? 0, dp);
    }
    return out;
  }, [pairs]);
  const target = network?.nodes.find((x) => x.id === tpl?.target);

  const pushLive = async () => {
    if (!tpl) return;
    try {
      await pushDisruption(tpl.template);
      setPushed(tpl.template);
      await useLive.getState().refreshNetwork();
      toast.success("Pushed to the live twin", { description: `${tpl.name ?? tpl.type} is now active on the Control Tower map`,
        action: { label: "View on map", onClick: () => { selectNode(tpl.target); navigate("/"); } } });
    } catch (e) { toast.error("Could not push", { description: String(e) }); }
  };
  const mapLayers = network ? [
    ...liveLayers({ t: performance.now() / 1000, pulse: t, net: network, vehicles: [...liveVehicles.values()], inventory, ports,
      layers: { ...layers, inventory: false }, selectedNode: tpl?.target ?? null, version: dataVersion,
      impact: st?.impact ?? null, risk: res ? risk : undefined, planShipments }),
    ...(tpl?.polygon ? [new PolygonLayer({ id: "sc-poly", data: [{ p: tpl.polygon }], getPolygon: (d: { p: [number, number][] }) => d.p,
      getFillColor: [239, 68, 68, 40], getLineColor: [239, 68, 68, 220], lineWidthMinPixels: 2, stroked: true })] : []),
    ...(target ? [new ScatterplotLayer({ id: "sc-target", data: [target], getPosition: (d: { lon: number; lat: number }) => [d.lon, d.lat],
      getRadius: 18 + 8 * Math.sin(t * 3), radiusUnits: "pixels", filled: false, stroked: true, getLineColor: [239, 68, 68, 230], lineWidthMinPixels: 2.5,
      updateTriggers: { getRadius: t } })] : []),
  ] : [];

  return (
    <div className="absolute inset-0 pt-14 px-4 pb-4 flex gap-3">
      <Glass className="w-[232px] shrink-0 flex flex-col overflow-hidden">
        <div className="px-4 pt-3.5 pb-2">
          <div className="text-[13px] font-semibold">Disruption templates</div>
          <div className="text-[11.5px] text-ink-3 mt-0.5">Scenario DSL · live API</div>
        </div>
        <div className="flex-1 overflow-y-auto scroll-thin px-2 pb-2 space-y-1">
          {templates.map((x) => {
            const Icon = ICONS[x.type] ?? Tornado;
            const active = x.template === sel;
            return (
              <button key={x.template} onClick={() => setSel(x.template)}
                className={clsx("w-full text-left flex items-center gap-3 rounded-xl px-2.5 py-2.5 border transition cursor-pointer",
                  active ? "border-bad/50 bg-bad/8" : "border-transparent hover:border-line-strong hover:bg-white/4")}>
                <span className={clsx("w-8 h-8 rounded-lg grid place-items-center shrink-0", active ? "bg-bad/20 text-bad" : "bg-white/5 text-ink-2")}><Icon size={16} /></span>
                <span className="min-w-0 flex-1">
                  <span className="block text-[12.5px] font-medium text-ink truncate">{x.name ?? x.type}</span>
                  <span className="num block text-[10.5px] text-ink-3 truncate">{x.target} · {x.duration_h} h · sev {x.severity}</span>
                </span>
              </button>
            );
          })}
        </div>
        {tpl && (
          <div className="p-3 border-t border-line space-y-2.5">
            <p className="text-[11px] text-ink-3 leading-snug line-clamp-3">{tpl.description}</p>
            <div className="grid grid-cols-2 gap-2 text-[11px] text-ink-3">
              <label>Runs<input type="number" min={10} max={1000} step={10} value={n} onChange={(e) => setN(Number(e.target.value))}
                className="num mt-1 w-full rounded-md bg-white/5 border border-line px-2 py-1 text-ink" /></label>
              <label>Days<input type="number" min={5} max={90} value={days} onChange={(e) => setDays(Number(e.target.value))}
                className="num mt-1 w-full rounded-md bg-white/5 border border-line px-2 py-1 text-ink" /></label>
            </div>
            <Button variant="primary" className="w-full" disabled={!!running}
              onClick={() => { if (!ensureRole("planner")) return; run(sel, n, days).catch((e) => toast.error("Could not start", { description: String(e) })); }}>
              {running ? <Loader2 size={13} className="animate-spin" /> : <Play size={13} />} {running ? "Running…" : `Run ${n} simulations`}
            </Button>
          </div>
        )}
      </Glass>

      <div className="flex-1 min-w-0 flex flex-col gap-3">
        <div className="relative flex-[1.1] min-h-0 rounded-2xl overflow-hidden border border-line">
          <DeckMap initialViewState={{ longitude: 79.5, latitude: 17.5, zoom: 4.4, pitch: 30, bearing: 0 }} layers={mapLayers} className="absolute inset-0" />
          {st && (
            <div className="absolute left-3 top-3 glass !bg-[#0c1322]/92 p-3.5 w-[300px]">
              <div className="flex items-center gap-2"><Badge sev="bad">{tpl?.type ?? "scenario"}</Badge>
                <span className="num text-[11px] text-ink-3 ml-auto">{st.id}{st.cached ? " · cached" : ""}</span></div>
              <div className="mt-3 flex items-center gap-3">
                <ProgressRing value={st.n ? st.done / st.n : 0} size={56} stroke={5}>
                  <span className="num text-[11px] text-ink">{Math.round((st.n ? st.done / st.n : 0) * 100)}%</span>
                </ProgressRing>
                <div>
                  <div className="num text-[15px] text-ink"><Ticker value={st.done} duration={150} /> / {st.n}</div>
                  <div className="text-[11px] text-ink-3">{st.status === "done" ? `done in ${(res?.wall_s ?? st.wall_s ?? 0).toFixed(1)} s` : "Monte Carlo replications"}</div>
                </div>
              </div>
              {st.impact && res && (
                <div className="num mt-2.5 text-[11px] text-ink-3 leading-relaxed">
                  <span className="text-bad">{Object.keys(st.impact.nodes).length} node(s)</span> · <span className="text-bad">{Object.keys(st.impact.lanes).length} lane(s)</span> disrupted ·{" "}
                  <span className="text-warn">{Object.values(risk).filter((p) => p > 0.005).length} DC(s)</span> at added stock-out risk
                </div>
              )}
              {res && (
                <Button variant={pushed === tpl?.template ? "ghost" : "primary"} className="w-full mt-2.5" disabled={pushed === tpl?.template} onClick={pushLive}>
                  <Zap size={13} /> {pushed === tpl?.template ? "Active in the live twin" : "Push to live twin"}
                </Button>
              )}
            </div>
          )}
        </div>

        <div className="flex-1 min-h-0 grid grid-cols-[1.4fr_1fr] gap-3">
          <Glass className="p-3.5 flex flex-col min-h-0">
            <div className="flex items-center gap-2">
              <Eyebrow>On-hand stock · P10–P90</Eyebrow>
              {res && (
                <select value={pair ?? ""} onChange={(e) => setPair(e.target.value)}
                  className="num ml-auto max-w-[260px] rounded-md bg-white/5 border border-line px-2 py-0.5 text-[11px] text-ink">
                  {pairs.map(([k, dp, drop]) => (
                    <option key={k} value={k}>{k}{dp > 0 ? ` · +${(dp * 100).toFixed(0)} pp stock-out` : drop > 0.02 ? ` · −${(drop * 100).toFixed(0)}% stock` : ""}</option>
                  ))}
                </select>
              )}
            </div>
            <div className="flex-1 min-h-0 mt-1">
              {res && pair && res.series[pair] ? <FanChart st={st!} base={base} pair={pair} /> :
                <div className="h-full grid place-items-center text-[12px] text-ink-3">{running ? "Simulating…" : "Pick a template and run it"}</div>}
            </div>
          </Glass>

          <Glass className="p-3.5 flex flex-col min-h-0 overflow-y-auto scroll-thin">
            <div className="flex items-center"><Eyebrow>KPI deltas · P50 vs baseline</Eyebrow>
              {res && <Button variant="ghost" className="ml-auto" disabled={optimizing} onClick={optimize}>
                {optimizing ? <Loader2 size={13} className="animate-spin" /> : <Sparkles size={13} />} Optimise</Button>}
            </div>
            {st?.deltas ? (
              <table className="mt-2 w-full text-[12px]">
                <tbody>
                  {KPI_ROWS.map((r) => {
                    const d = st.deltas![r.key];
                    if (!d) return null;
                    const worse = r.good === "up" ? d.delta_p50 < -1e-9 : d.delta_p50 > 1e-9;
                    const better = r.good === "up" ? d.delta_p50 > 1e-9 : d.delta_p50 < -1e-9;
                    return (
                      <tr key={r.key} className="border-b border-line/60">
                        <td className="py-1.5 text-ink-2">{r.label}</td>
                        <td className="num text-right text-ink">{r.fmt(d.scenario_p50)}</td>
                        <td className={clsx("num text-right w-[92px]", worse ? "text-bad" : better ? "text-good" : "text-ink-3")}>
                          {d.delta_p50 >= 0 ? "+" : "−"}{r.fmt(Math.abs(d.delta_p50))}</td>
                      </tr>
                    );
                  })}
                </tbody>
              </table>
            ) : <div className="mt-3 text-[12px] text-ink-3">{res ? "Waiting for the baseline…" : "Deltas appear when the run and its baseline finish."}</div>}
            {res?.tts?.[0] && (
              <div className="num mt-2 text-[11px] text-ink-3">
                TTR {res.tts[0].ttr_h ? (res.tts[0].ttr_h / 24).toFixed(1) : "—"} d · P(stock-out) {(res.tts[0].p_stockout * 100).toFixed(0)}%
                {res.tts[0].tts_h ? ` · TTS P50 ${(res.tts[0].tts_h.p50 / 24).toFixed(1)} d` : ""} · P(exposed) {(res.tts[0].p_exposed * 100).toFixed(0)}%
              </div>
            )}
            {st?.plans && st.plans.length > 0 && <PlansPanel scenarioId={st.id} initial={st.plans} />}
          </Glass>
        </div>
      </div>
    </div>
  );
}
