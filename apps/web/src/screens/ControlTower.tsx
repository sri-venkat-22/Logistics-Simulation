import { useEffect, useMemo, useRef, useState } from "react";
import { AnimatePresence, motion } from "motion/react";
import { useNavigate } from "react-router";
import { Layers, X, ArrowDownLeft, ArrowUpRight, Clock3, History, Eye, Sparkles } from "lucide-react";
import clsx from "clsx";
import type { PickingInfo } from "@deck.gl/core";
// import { PathLayer } from "@deck.gl/layers";
import type { MapRef } from "react-map-gl/maplibre";
import { DeckMap, useAnimationClock, tooltipStyle } from "../components/DeckMap";
import { Badge, Bar, Button, Dot, Eyebrow, Glass, KpiCard } from "../components/ui";
import { Chart } from "../components/Chart";
import { control, network, nodeById, laneById, shipments, skuById, scenario, type NetNode, type Sev } from "../lib/data";
import { networkLayers, nodeTooltip, predictedStock } from "../lib/networkLayers";
import { useAegis } from "../lib/store";
import { useLive, vehicles as liveVehicles, type LiveVehicle } from "../lib/live";
import { liveLayers } from "../lib/liveLayers";
import type { ApiLane, ApiNode } from "../lib/api";
import { laneGeo, particles } from "../lib/networkLayers";
import { normalisedTimestamps } from "../lib/geo";
import { CameraPresets, LiveAlertFeed, LiveKpiStrip, LiveNodePanel, LiveStatusBar } from "./ControlTowerLive";
import { C, VIEW, chartBase, nodeTypeLabel, modeLabel, sevColor } from "../lib/theme";

const LAYER_LABELS: [string, string][] = [
  ["nodes", "Nodes"], ["lanes", "Lanes"], ["trucks", "Shipments"], ["ships", "Ships (AIS)"],
  ["inventory", "Inventory bars"], ["weather", "Weather"], ["risk", "Risk heatmap"],
];

function LayerPanel() {
  const { layers, toggleLayer } = useAegis();
  return (
    <Glass className="absolute left-4 top-[200px] w-[184px] py-2.5 z-10">
      <div className="flex items-center gap-2 px-3.5 pb-2"><Layers size={13} className="text-ink-3" /><Eyebrow>Layers</Eyebrow></div>
      {LAYER_LABELS.map(([k, label]) => (
        <label key={k} className="flex items-center justify-between px-3.5 h-8 text-[12.5px] text-ink-2 hover:text-ink cursor-pointer">
          {label}
          <button
            role="switch" aria-checked={layers[k]} onClick={() => toggleLayer(k)}
            className={clsx("relative w-7 h-4 rounded-full transition cursor-pointer", layers[k] ? "bg-ok/70" : "bg-white/12")}
          >
            <span className={clsx("absolute top-0.5 w-3 h-3 rounded-full bg-white transition-all", layers[k] ? "left-3.5" : "left-0.5")} />
          </button>
        </label>
      ))}
      <div className="mx-3.5 mt-2 pt-2.5 border-t border-line grid grid-cols-2 gap-y-1.5 text-[10.5px] text-ink-3">
        {([["ok", "Healthy"], ["warn", "At risk"], ["bad", "Disrupted"], ["ai", "AI plan"]] as [Sev, string][]).map(([s, l]) => (
          <span key={s} className="flex items-center gap-1.5"><Dot sev={s} size={6} />{l}</span>
        ))}
      </div>
    </Glass>
  );
}

const SEV_ICON: Record<Sev, string> = { ok: "OK", good: "OK", warn: "RISK", bad: "ALERT", ai: "AI", sec: "SEC" };

function AlertFeed() {
  const { selectNode } = useAegis();
  const navigate = useNavigate();
  return (
    <Glass className="absolute right-4 top-[150px] bottom-[104px] w-[330px] z-10 flex flex-col overflow-hidden">
      <div className="flex items-center justify-between px-4 pt-3.5 pb-2">
        <div className="text-[13px] font-semibold">Alerts</div>
        <span className="num text-[11px] text-ink-3">{control.alerts.length} open</span>
      </div>
      <div className="flex-1 overflow-y-auto scroll-thin px-2 pb-2">
        {control.alerts.map((a, i) => (
          <motion.button
            key={a.id}
            initial={{ opacity: 0, y: 8 }} animate={{ opacity: 1, y: 0 }} transition={{ delay: 0.05 * i, duration: 0.3 }}
            onClick={() => (a.sev === "ai" ? navigate("/scenario") : a.sev === "sec" ? navigate("/trust") : a.node && selectNode(a.node))}
            className="w-full text-left rounded-xl px-2.5 py-2.5 hover:bg-white/4 transition cursor-pointer flex gap-3"
          >
            <span className="mt-1"><Dot sev={a.sev} pulse={a.sev === "bad"} /></span>
            <span className="min-w-0 flex-1">
              <span className="flex items-center gap-2">
                <span className="text-[12.5px] font-medium text-ink truncate">{a.title}</span>
              </span>
              <span className="block text-[11.5px] leading-snug text-ink-3 mt-0.5">{a.body}</span>
              <span className="flex items-center gap-2 mt-1.5">
                <Badge sev={a.sev}>{SEV_ICON[a.sev]}</Badge>
                <span className="text-[10.5px] text-ink-3">{a.t}</span>
              </span>
            </span>
          </motion.button>
        ))}
      </div>
    </Glass>
  );
}

function TimelineScrubber() {
  const { timeOffsetH, setTimeOffset } = useAegis();
  const { hours, inr_at_risk_cr } = control.timeline;
  const label = timeOffsetH === 0 ? "NOW" : timeOffsetH < 0 ? `${timeOffsetH} h` : `+${timeOffsetH} h`;
  const mode = timeOffsetH < 0 ? "past" : timeOffsetH > 0 ? "future" : "now";
  const option = useMemo(() => ({
    ...chartBase,
    grid: { left: 0, right: 0, top: 2, bottom: 0 },
    tooltip: { show: false },
    xAxis: { type: "value" as const, min: -48, max: 72, show: false },
    yAxis: { type: "value" as const, show: false, min: 0 },
    series: [{
      type: "line" as const, data: hours.map((h, i) => [h, inr_at_risk_cr[i]]), showSymbol: false, smooth: true,
      lineStyle: { width: 1.2, color: C.ink3 },
      areaStyle: { color: { type: "linear" as const, x: 0, y: 0, x2: 0, y2: 1, colorStops: [{ offset: 0, color: "rgba(245,158,11,0.25)" }, { offset: 1, color: "rgba(245,158,11,0)" }] } },
      markArea: { silent: true, itemStyle: { color: "rgba(167,139,250,0.06)" }, data: [[{ xAxis: 0 }, { xAxis: 72 }]] },
    }],
  }), [hours, inr_at_risk_cr]);
  return (
    <Glass className="absolute left-4 right-4 bottom-4 h-[76px] z-10 px-4 py-2.5 flex items-center gap-4">
      <div className="w-[118px] shrink-0">
        <Eyebrow>Timeline</Eyebrow>
        <div className="mt-1 flex items-center gap-1.5">
          {mode === "past" ? <History size={14} className="text-ink-2" /> : mode === "future" ? <Eye size={14} className="text-ai" /> : <Clock3 size={14} className="text-ok" />}
          <span className={clsx("num text-[15px] font-semibold", mode === "future" ? "text-ai" : "text-ink")}>{label}</span>
        </div>
        <div className="text-[10.5px] text-ink-3 mt-0.5">{mode === "future" ? "Predicted · P50" : mode === "past" ? "Replay from history" : "Live"}</div>
      </div>
      <div className="relative flex-1 min-w-0 h-full">
        <div className="absolute inset-x-0 top-0 h-7 opacity-80"><Chart option={option} height={28} /></div>
        <div className="absolute inset-x-0 top-[26px] flex justify-between text-[10px] num text-ink-3 px-0.5">
          <span>−48 h</span><span>−24 h</span><span className="text-ok">Past | Now | Future</span><span>+24 h</span><span>+48 h</span><span>+72 h</span>
        </div>
        <input
          aria-label="Timeline offset in hours" type="range" className="scrubber absolute inset-x-0 bottom-0"
          min={-48} max={72} step={2} value={timeOffsetH} onChange={(e) => setTimeOffset(Number(e.target.value))}
        />
        <div className="absolute bottom-[14px] w-px h-9 bg-ok/60 pointer-events-none" style={{ left: `${(48 / 120) * 100}%` }} />
      </div>
      <div className="w-[170px] shrink-0 text-right">
        {mode === "future" ? (
          <>
            <div className="eyebrow">Shamshabad · vaccines</div>
            <div className="num text-[15px] mt-1" style={{ color: predictedStock(timeOffsetH) < scenario.safety_stock ? C.warn : C.ink }}>
              {predictedStock(timeOffsetH).toLocaleString("en-IN")} <span className="text-[11px] text-ink-3">units P50</span>
            </div>
          </>
        ) : (
          <Button variant="ghost" onClick={() => setTimeOffset(24)}>Look 24 h ahead →</Button>
        )}
      </div>
    </Glass>
  );
}

function Gauge({ label, value, safety, capacity }: { label: string; value: number; safety: number; capacity: number }) {
  const state: Sev = value < safety ? "bad" : value < safety * 1.6 ? "warn" : "ok";
  return (
    <div>
      <div className="flex justify-between text-[12px]">
        <span className="text-ink-2">{label}</span>
        <span className="num text-ink">{value.toLocaleString("en-IN")} <span className="text-ink-3">/ {capacity.toLocaleString("en-IN")}</span></span>
      </div>
      <div className="mt-1.5"><Bar value={value} max={capacity} color={sevColor[state]} marker={safety} /></div>
      <div className="mt-1 text-[10.5px] text-ink-3">Safety stock {safety.toLocaleString("en-IN")} ·{" "}
        <span style={{ color: sevColor[state] }}>{state === "ok" ? "healthy" : state === "warn" ? "approaching safety" : "below safety"}</span></div>
    </div>
  );
}

function NodeSlideOver({ node }: { node: NetNode }) {
  const { selectNode, timeOffsetH } = useAegis();
  const inv = network.inventory[node.id];
  const inbound = shipments.filter((s) => laneById.get(s.lane_id)!.to_id === node.id).slice(0, 5);
  const outbound = shipments.filter((s) => laneById.get(s.lane_id)!.from_id === node.id).slice(0, 5);
  const exposed = node.tts_d !== null && node.ttr_d !== null && node.tts_d < node.ttr_d;
  const maxD = Math.max(node.tts_d ?? 0, node.ttr_d ?? 0, 1) * 1.15;
  return (
    <motion.aside
      initial={{ x: 380, opacity: 0 }} animate={{ x: 0, opacity: 1 }} exit={{ x: 380, opacity: 0 }}
      transition={{ duration: 0.3, ease: [0.22, 1, 0.36, 1] }}
      className="absolute right-4 top-[150px] bottom-[104px] w-[360px] z-20 glass !bg-[#0c1322]/94 flex flex-col overflow-hidden shadow-2xl"
    >
      <div className="flex items-start gap-3 px-4 pt-4 pb-3 border-b border-line">
        <div className="min-w-0">
          <Eyebrow>{nodeTypeLabel[node.type]}</Eyebrow>
          <div className="text-[15px] font-semibold mt-1 leading-snug">{node.name}</div>
          <div className="num text-[11px] text-ink-3 mt-1">{node.id} · {node.lat.toFixed(3)}, {node.lon.toFixed(3)}</div>
        </div>
        <button onClick={() => selectNode(null)} className="ml-auto text-ink-3 hover:text-ink cursor-pointer" aria-label="Close"><X size={16} /></button>
      </div>
      <div className="flex-1 overflow-y-auto scroll-thin px-4 py-4 space-y-5">
        {node.tts_d !== null && (
          <section>
            <div className="flex items-center justify-between"><Eyebrow>Time-to-survive vs time-to-recover</Eyebrow>{exposed && <Badge sev="warn">Exposed</Badge>}</div>
            <div className="mt-3 space-y-2.5">
              {[["TTS", node.tts_d!, exposed ? C.warn : C.ok], ["TTR", node.ttr_d!, C.ink3]].map(([k, v, c]) => (
                <div key={k as string} className="flex items-center gap-3">
                  <span className="num w-8 text-[11px] text-ink-3">{k}</span>
                  <div className="flex-1"><Bar value={v as number} max={maxD} color={c as string} height={8} /></div>
                  <span className="num w-12 text-right text-[12.5px]">{(v as number).toFixed(1)} d</span>
                </div>
              ))}
            </div>
            <p className="text-[11.5px] text-ink-3 mt-2 leading-snug">
              {exposed ? "This node runs out before its supply recovers from a worst-case disruption (Simchi-Levi TTS < TTR)." : "This node can ride out its worst-case supply disruption."}
            </p>
          </section>
        )}
        {inv && (
          <section>
            <Eyebrow>Inventory by SKU {timeOffsetH > 0 && <span className="text-ai normal-case tracking-normal">· predicted +{timeOffsetH} h</span>}</Eyebrow>
            <div className="mt-3 space-y-4">
              {inv.map((r) => {
                const v = node.id === "DC_HYD_SHAMSHABAD" && r.sku === "SKU_VAX" && timeOffsetH > 0 ? predictedStock(timeOffsetH) : r.on_hand;
                return <Gauge key={r.sku} label={skuById.get(r.sku)!.name} value={v} safety={r.safety} capacity={r.capacity} />;
              })}
            </div>
          </section>
        )}
        {[["Inbound", inbound, ArrowDownLeft], ["Outbound", outbound, ArrowUpRight]].map(([label, list, Icon]) => (
          (list as typeof shipments).length > 0 && (
            <section key={label as string}>
              <Eyebrow>{label as string} shipments</Eyebrow>
              <div className="mt-2 divide-y divide-line">
                {(list as typeof shipments).map((s) => {
                  const lane = laneById.get(s.lane_id)!;
                  const other = nodeById.get(label === "Inbound" ? lane.from_id : lane.to_id)!;
                  const I = Icon as typeof ArrowDownLeft;
                  return (
                    <div key={s.id} className="flex items-center gap-2.5 py-2 text-[12px]">
                      <I size={13} className="text-ink-3 shrink-0" />
                      <span className="num text-ink-2 w-[68px]">{s.id}</span>
                      <span className="truncate flex-1 text-ink-3">{modeLabel[lane.mode]} · {other.name.split(" (")[0]}</span>
                      <span className="num text-ink">{s.eta_h.toFixed(0)} h</span>
                      <Dot sev={s.status === "in_transit" ? "ok" : "warn"} size={6} />
                    </div>
                  );
                })}
              </div>
            </section>
          )
        ))}
      </div>
    </motion.aside>
  );
}

/** Frames per second over the last second (render-loop health for NFR-4). */
function useFps(t: number) {
  const r = useRef({ n: 0, last: 0, fps: 0, lastT: -1 });
  if (t !== r.current.lastT) { r.current.n += 1; r.current.lastT = t; }  // StrictMode renders twice per frame in dev
  if (t - r.current.last >= 1) { r.current.fps = Math.round(r.current.n / (t - r.current.last)); r.current.n = 0; r.current.last = t; }
  return r.current.fps;
}

function LiveTower() {
  const t = useAnimationClock();
  const fps = useFps(t);
  const mapRef = useRef<MapRef>(null);
  const { layers, selectedNode, selectNode } = useAegis();
  const { network, inventory, ports, dataVersion } = useLive();
  const [zoom, setZoom] = useState<number>(VIEW.INDIA.zoom);
  useEffect(() => {
    const id = window.setInterval(() => { const z = mapRef.current?.getZoom(); if (z !== undefined) setZoom(Math.round(z * 4) / 4); }, 250);
    return () => window.clearInterval(id);
  }, []);
  const now = performance.now() / 1000;
  const [, setRealRoadsLoaded] = useState(false);

  useEffect(() => {
    let mounted = true;
    async function loadRoads() {
      const roadLanes = laneGeo.filter(g => g.lane.mode === "road" && g.path.length === 50);
      const promises = roadLanes.map(async (g) => {
        const a = nodeById.get(g.lane.from_id)!;
        const b = nodeById.get(g.lane.to_id)!;
        const url = `https://router.project-osrm.org/route/v1/driving/${a.lon.toFixed(5)},${a.lat.toFixed(5)};${b.lon.toFixed(5)},${b.lat.toFixed(5)}?overview=full&geometries=geojson`;
        try {
          const res = await fetch(url);
          const data = await res.json();
          if (data.routes && data.routes.length > 0) {
            const coords = data.routes[0].geometry.coordinates;
            g.path = coords.map((c: any) => [c[0], c[1], 0]);
            g.ts = normalisedTimestamps(g.path);

            for (const p of particles) {
              if (p.laneId === g.lane.id) {
                p.path = g.path;
                p.timestamps = g.ts.map(t => p.timestamps[0] + t * (p.timestamps[p.timestamps.length - 1] - p.timestamps[0]));
              }
            }
          }
        } catch (e) {
          // ignore rate limits or network issues for individual routes
        }
      });
      await Promise.allSettled(promises);
      if (mounted) setRealRoadsLoaded(true);
    }
    loadRoads();
    return () => { mounted = false; };
  }, []);

  const [vs, setVs] = useState<any>(VIEW.INDIA);

  const layerList = network ? liveLayers({
    t: now, pulse: t, net: network, vehicles: [...liveVehicles.values()], inventory, ports, layers,
    selectedNode, version: dataVersion, zoom
  }) : [];


  return (
    <div className="absolute inset-0">
      <DeckMap
        ref={mapRef}
        globe
        viewState={vs}
        onMove={v => setVs(v)}
        layers={layerList}
        onClick={(info: PickingInfo) => {
          const o = info.object as ApiNode | undefined;
          if (o && "degree" in o) selectNode(o.id); else if (!info.object) selectNode(null);
        }}
        getTooltip={(info) => {
          const o = info.object as Record<string, unknown> | undefined;
          if (!o) return null;
          if ("degree" in o) {
            const n = o as unknown as ApiNode;
            const inv = Object.entries(inventory).filter(([k]) => k.startsWith(`${n.id}/`))
              .map(([k, v]) => `<div style="display:flex;justify-content:space-between;gap:14px"><span style="color:#9AA7BD">${k.split("/")[1].replace("SKU_", "")}</span><span style="font-family:JetBrains Mono Variable,monospace">${Math.round(v.on_hand).toLocaleString("en-IN")}</span></div>`).join("");
            return { html: `<div style="font-weight:600;margin-bottom:4px">${n.name}</div><div style="color:#5D6A82;font-size:11px;margin-bottom:4px">${n.id} · ${n.type} · ${ports[n.id]?.status ?? n.status}</div>${inv}`, style: tooltipStyle };
          }
          if ("trail" in o) {
            const v = o as unknown as LiveVehicle;
            return { html: `<div style="font-weight:600">${v.id}</div><div style="color:#9AA7BD">${v.scope === "city" ? "Hyderabad · SUMO" : "national road"} · ${v.status}</div><div style="font-family:JetBrains Mono Variable,monospace;margin-top:4px">${v.speed.toFixed(0)} km/h · ${v.heading.toFixed(0)}°${v.shipment ? ` · ${v.shipment}` : ""}</div>`, style: tooltipStyle };
          }
          if ("live_mult" in o) {
            const l = o as unknown as ApiLane;
            return { html: `<div style="font-weight:600">${l.id} · ${modeLabel[l.mode]}</div><div style="font-family:JetBrains Mono Variable,monospace;margin-top:4px">${l.distance_km.toLocaleString("en-IN")} km · ${l.lt_mean_h} h mean${l.live_mult > 1.001 ? ` · ×${l.live_mult.toFixed(2)}` : ""}</div>`, style: tooltipStyle };
          }
          return null;
        }}
      />
      <LiveKpiStrip />
      <CameraPresets mapRef={mapRef} />
      <LayerPanel />
      <AnimatePresence mode="wait">
        {selectedNode ? <LiveNodePanel key={selectedNode} id={selectedNode} /> : <LiveAlertFeed key="alerts" />}
      </AnimatePresence>
      <LiveStatusBar fps={fps} />
    </div>
  );
}

export default function ControlTower() {
  const offline = useLive((s) => s.status === "offline" && !s.network);  // fall back to the prototype only when the API is down
  return offline ? <MockTower /> : <LiveTower />;
}

/** Level-1 prototype view on seeded mock data (used when the API is not reachable). */
function MockTower() {

  const t = useAnimationClock();
  const { layers, selectedNode, selectNode, timeOffsetH, appliedPlan } = useAegis();
  const node = selectedNode ? nodeById.get(selectedNode) : undefined;
  const layerList = networkLayers({ t, timeOffsetH, layers, selectedNode, mitigated: appliedPlan === "A" });
  const future = timeOffsetH > 0;

  return (
    <div className="absolute inset-0">
      <DeckMap
        initialViewState={VIEW.INDIA}
        layers={layerList}
        onClick={(info: PickingInfo) => {
          const o = info.object as NetNode | undefined;
          if (o && "type" in o && "betweenness" in o) selectNode(o.id); else if (!info.object) selectNode(null);
        }}
        getTooltip={(info) => {
          const o = info.object as Record<string, unknown> | undefined;
          if (!o) return null;
          if ("betweenness" in o) return { html: nodeTooltip(o as unknown as NetNode), style: tooltipStyle };
          if ("lane_id" in o) {
            const s = o as unknown as (typeof shipments)[number];
            const lane = laneById.get(s.lane_id)!;
            return { html: `<div style="font-weight:600">${s.id} · ${skuById.get(s.sku_id)!.name}</div><div style="color:#9AA7BD;margin-top:2px">${nodeById.get(lane.from_id)!.name.split(" (")[0]} → ${nodeById.get(lane.to_id)!.name.split(" (")[0]}</div><div style="font-family:JetBrains Mono Variable,monospace;margin-top:4px">ETA ${s.eta_h} h <span style="color:#5D6A82">(P10 ${s.eta_p10_h} – P90 ${s.eta_p90_h})</span></div>`, style: tooltipStyle };
          }
          if ("lane" in o) {
            const l = (o as { lane: (typeof network.lanes)[number] }).lane;
            return { html: `<div style="font-weight:600">${l.id} · ${modeLabel[l.mode]}</div><div style="color:#9AA7BD">${nodeById.get(l.from_id)!.name.split(" (")[0]} → ${nodeById.get(l.to_id)!.name.split(" (")[0]}</div><div style="font-family:JetBrains Mono Variable,monospace;margin-top:4px">${l.distance_km.toLocaleString("en-IN")} km · ${l.lt_mean_h} h mean</div>`, style: tooltipStyle };
          }
          if ("wind_kmh" in o) return { html: `<b>${o.name}</b><div style="font-family:JetBrains Mono Variable,monospace">wind ${o.wind_kmh} km/h · rain ${o.rain_mm} mm</div><div style="color:#5D6A82;font-size:11px">Open-Meteo</div>`, style: tooltipStyle };
          if ("heading" in o) return { html: `<b>${o.id}</b><div style="color:#5D6A82;font-size:11px">AIS vessel</div>`, style: tooltipStyle };
          return null;
        }}
      />
      {future && <div className="absolute inset-0 pointer-events-none bg-[radial-gradient(ellipse_at_center,transparent_40%,rgba(167,139,250,0.10))] ring-1 ring-inset ring-ai/20" />}

      <div className="absolute left-4 right-4 top-14 z-10 grid grid-cols-4 gap-3 max-w-[1100px]">
        {control.kpis.map((k) => (
          <KpiCard key={k.id} label={k.label} value={k.value} unit={k.unit} delta={k.delta} state={k.state} spark={k.spark}
            decimals={k.unit === "%" || k.unit === "Cr" ? 1 : 0} prefix={k.id === "inr" ? "₹" : ""} />
        ))}
      </div>

      {appliedPlan === "A" && (
        <div className="absolute left-[212px] top-[150px] z-10 glass px-3 py-2 flex items-center gap-2 text-[12px] border-ai/40">
          <Sparkles size={14} className="text-ai" /> Plan A active · reroute via Vizag + BLR transfer
        </div>
      )}

      <LayerPanel />
      <AnimatePresence mode="wait">
        {node ? <NodeSlideOver key={node.id} node={node} /> : <AlertFeed key="alerts" />}
      </AnimatePresence>
      <TimelineScrubber />
    </div>
  );
}
