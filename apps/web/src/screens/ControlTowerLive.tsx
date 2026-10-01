/** Live-data pieces of the Control Tower (Phase 5): KPI strip, alert feed, node drill-down, camera presets, status bar. */
import { useEffect, useMemo, useState } from "react";
import { motion } from "motion/react";
import { useNavigate } from "react-router";
import { Globe2, Map as MapIcon, Building2, Radio, X } from "lucide-react";
import clsx from "clsx";
import type { MapRef } from "react-map-gl/maplibre";
import { Badge, Bar, Button, Dot, Eyebrow, Glass, KpiCard } from "../components/ui";
import { api, endDisruption, type ApiNode } from "../lib/api";
import { toast } from "sonner";
import { Zap } from "lucide-react";
import { useLive, type LiveAlert } from "../lib/live";
import { useAegis } from "../lib/store";
import { C, VIEW, nodeTypeLabel, sevColor } from "../lib/theme";
import type { Sev } from "../lib/data";

export function LiveKpiStrip() {
  const { kpis, kpiHistory } = useLive();
  if (!kpis) return null;
  const hasDemand = (kpis.twin?.units_demanded ?? 0) > 0;
  const fill = hasDemand ? (kpis.twin?.fill_rate ?? 0) * 100 : NaN;
  const cards = [
    { id: "fill", label: "Fill rate (twin)", value: hasDemand ? fill : 100, unit: hasDemand ? "%" : "% · no orders yet", decimals: 1,
      state: (!hasDemand || fill >= 97 ? "ok" : fill >= 90 ? "warn" : "bad") as Sev, spark: (kpiHistory.fill ?? []).filter((x) => x > 0) },
    { id: "rate", label: "Telemetry ingest", value: kpis.ingest_rate, unit: "msg/s", decimals: 0, state: (kpis.ingest_rate > 0 ? "ok" : "warn") as Sev, spark: kpiHistory.rate ?? [] },
    { id: "vehicles", label: "Vehicles reporting", value: kpis.vehicles_live, unit: kpis.vehicles_predicted ? `+${kpis.vehicles_predicted} predicted` : "live", decimals: 0, state: (kpis.vehicles_predicted ? "warn" : "ok") as Sev, spark: kpiHistory.vehicles ?? [] },
    { id: "quarantined", label: "Quarantined messages", value: kpis.quarantined, unit: "total", decimals: 0, state: (kpis.quarantined ? "warn" : "ok") as Sev, spark: kpiHistory.quarantined ?? [] },
  ];
  return (
    <div className="absolute left-4 right-4 top-14 z-10 grid grid-cols-4 gap-3 max-w-[1100px]">
      {cards.map((k) => (
        <KpiCard key={k.id} label={k.label} value={k.value} unit={k.unit} delta={0} state={k.state} spark={k.spark.length > 1 ? k.spark : [k.value, k.value]} decimals={k.decimals} />
      ))}
    </div>
  );
}

const SEV_LABEL: Record<Sev, string> = { ok: "OK", good: "OK", warn: "RISK", bad: "ALERT", ai: "AI", sec: "SEC" };

export function LiveAlertFeed() {
  const alerts = useLive((s) => s.alerts);
  const { selectNode } = useAegis();
  const navigate = useNavigate();
  return (
    <Glass className="absolute right-4 top-[150px] bottom-[104px] w-[330px] z-10 flex flex-col overflow-hidden">
      <div className="flex items-center justify-between px-4 pt-3.5 pb-2">
        <div className="text-[13px] font-semibold">Alerts</div>
        <span className="num text-[11px] text-ink-3">{alerts.length} recent</span>
      </div>
      <div className="flex-1 overflow-y-auto scroll-thin px-2 pb-2">
        {alerts.length === 0 && <div className="px-3 py-6 text-[12px] text-ink-3">No alerts yet. They appear as telemetry reveals stock at risk, port changes, quarantined data or silent sources.</div>}
        {alerts.map((a: LiveAlert) => (
          <motion.button
            key={a.id} layout initial={{ opacity: 0, y: 8 }} animate={{ opacity: 1, y: 0 }} transition={{ duration: 0.25 }}
            onClick={() => (a.sev === "sec" ? navigate("/trust") : a.node ? selectNode(a.node) : undefined)}
            className="w-full text-left rounded-xl px-2.5 py-2.5 hover:bg-white/4 transition cursor-pointer flex gap-3"
          >
            <span className="mt-1"><Dot sev={a.sev} pulse={a.sev === "bad"} /></span>
            <span className="min-w-0 flex-1">
              <span className="block text-[12.5px] font-medium text-ink truncate">{a.title}</span>
              <span className="block text-[11.5px] leading-snug text-ink-3 mt-0.5">{a.body}</span>
              <span className="flex items-center gap-2 mt-1.5">
                <Badge sev={a.sev}>{SEV_LABEL[a.sev]}</Badge>
                <span className="num text-[10.5px] text-ink-3">{a.t ? a.t.slice(5, 16).replace("T", " ") : ""}</span>
              </span>
            </span>
          </motion.button>
        ))}
      </div>
    </Glass>
  );
}

interface NodeDetail {
  id: string; type: ApiNode["type"]; name: string; lat: number; lon: number;
  inventory: { sku: string; observed: { on_hand: number; on_order: number; backlog: number; s?: number } | null;
    twin: { on_hand: number; s: number; S: number; capacity: number; backlog: number } | null }[];
  inbound: { id: string; sku: string; qty: number; mode: string; from: string; status: string; eta_leg_h: number }[];
  outbound: { id: string; sku: string; qty: number; mode: string; to: string; status: string; eta_leg_h: number }[];
  tts_ttr: { tts_d: number | null; ttr_d: number | null; exposed: boolean | null; rei: number | null };
  port: { status: string; anchorage: number; berth_queue: number; berths_busy: number; berths_total: number } | null;
  twin: { status: string; anchorage?: number; berth_queue?: number; customs?: number } | null;
}

export function LiveNodePanel({ id }: { id: string }) {
  const { selectNode } = useAegis();
  const frames = useLive((s) => s.frames);
  const [d, setD] = useState<NodeDetail | null>(null);
  useEffect(() => {
    let dead = false;
    api<NodeDetail>(`/api/v1/nodes/${id}`).then((x) => { if (!dead) setD(x); }).catch(() => undefined);
    return () => { dead = true; };
  }, [id, Math.floor(frames / 10)]); // eslint-disable-line react-hooks/exhaustive-deps
  if (!d) return null;
  const { tts_d, ttr_d } = d.tts_ttr;
  const maxD = Math.max(tts_d ?? 0, ttr_d ?? 0, 1) * 1.15;
  return (
    <motion.aside
      initial={{ x: 380, opacity: 0 }} animate={{ x: 0, opacity: 1 }} exit={{ x: 380, opacity: 0 }}
      transition={{ duration: 0.3, ease: [0.22, 1, 0.36, 1] }}
      className="absolute right-4 top-[150px] bottom-[104px] w-[360px] z-20 glass !bg-[#0c1322]/94 flex flex-col overflow-hidden shadow-2xl"
    >
      <div className="flex items-start gap-3 px-4 pt-4 pb-3 border-b border-line">
        <div className="min-w-0">
          <Eyebrow>{nodeTypeLabel[d.type]} · live</Eyebrow>
          <div className="text-[15px] font-semibold mt-1 leading-snug">{d.name}</div>
          <div className="num text-[11px] text-ink-3 mt-1">{d.id} · {d.lat.toFixed(3)}, {d.lon.toFixed(3)}</div>
        </div>
        <button onClick={() => selectNode(null)} className="ml-auto text-ink-3 hover:text-ink cursor-pointer" aria-label="Close"><X size={16} /></button>
      </div>
      <div className="flex-1 overflow-y-auto scroll-thin px-4 py-4 space-y-5">
        {tts_d !== null && ttr_d !== null && (
          <section>
            <div className="flex items-center justify-between"><Eyebrow>Time-to-survive vs time-to-recover</Eyebrow>{d.tts_ttr.exposed && <Badge sev="warn">Exposed</Badge>}</div>
            <div className="mt-3 space-y-2.5">
              {([["TTS", tts_d, d.tts_ttr.exposed ? C.warn : C.ok], ["TTR", ttr_d, C.ink3]] as [string, number, string][]).map(([k, v, c]) => (
                <div key={k} className="flex items-center gap-3">
                  <span className="num w-8 text-[11px] text-ink-3">{k}</span>
                  <div className="flex-1"><Bar value={v} max={maxD} color={c} height={8} /></div>
                  <span className="num w-12 text-right text-[12.5px]">{v.toFixed(1)} d</span>
                </div>
              ))}
            </div>
            <p className="text-[11px] text-ink-3 mt-2">Simchi-Levi stress test (docs/sim/resilience.json) · REI {d.tts_ttr.rei?.toFixed(2)}</p>
          </section>
        )}
        {d.twin && d.twin.status !== "up" && (
          <section>
            <div className="flex items-center justify-between"><Eyebrow>Live twin</Eyebrow><Badge sev={d.twin.status === "closed" ? "bad" : "warn"}>{d.twin.status}</Badge></div>
            <div className="num mt-2 text-[12px] text-ink-2">
              {d.twin.anchorage !== undefined ? `${d.twin.anchorage} shipments at anchorage · ${d.twin.berth_queue} waiting for a berth · ${d.twin.customs} in customs` : "capacity reduced by an active disruption"}
            </div>
          </section>
        )}
        {d.port && (
          <section>
            <Eyebrow>Port status · observed</Eyebrow>
            <div className="num mt-2 text-[12px] text-ink-2">{d.port.status} · berths {d.port.berths_busy}/{d.port.berths_total} · queue {d.port.berth_queue} · anchorage {d.port.anchorage}</div>
          </section>
        )}
        {d.inventory.length > 0 && (
          <section>
            <Eyebrow>Inventory · observed vs twin</Eyebrow>
            <div className="mt-3 space-y-4">
              {d.inventory.map((r) => {
                const v = r.observed?.on_hand ?? r.twin?.on_hand ?? 0;
                const s = r.twin?.s ?? r.observed?.s ?? 0;
                const cap = Math.max(r.twin?.capacity ?? v * 1.5, v, 1);
                const st: Sev = (r.observed?.backlog ?? 0) > 0 ? "bad" : v < s ? "warn" : "ok";
                return (
                  <div key={r.sku}>
                    <div className="flex justify-between text-[12px]">
                      <span className="text-ink-2">{r.sku.replace("SKU_", "")}</span>
                      <span className="num text-ink">{Math.round(v).toLocaleString("en-IN")} <span className="text-ink-3">· twin {Math.round(r.twin?.on_hand ?? 0).toLocaleString("en-IN")}</span></span>
                    </div>
                    <div className="mt-1.5"><Bar value={v} max={cap} color={sevColor[st]} marker={s} /></div>
                    <div className="mt-1 text-[10.5px] text-ink-3">Reorder point {Math.round(s).toLocaleString("en-IN")}{r.observed?.backlog ? ` · ${Math.round(r.observed.backlog)} backordered` : ""}</div>
                  </div>
                );
              })}
            </div>
          </section>
        )}
        {[["Inbound", d.inbound], ["Outbound", d.outbound]].map(([label, list]) => (list as NodeDetail["inbound"]).length > 0 && (
          <section key={label as string}>
            <Eyebrow>{label as string} shipments · twin</Eyebrow>
            <div className="mt-2 divide-y divide-line">
              {(list as NodeDetail["inbound"]).slice(0, 8).map((s) => (
                <div key={s.id} className="flex items-center gap-2.5 py-2 text-[12px]">
                  <span className="num text-ink-2 w-[74px]">{s.id}</span>
                  <span className="truncate flex-1 text-ink-3">{s.mode} · {s.sku.replace("SKU_", "")} · {Math.round(s.qty).toLocaleString("en-IN")}</span>
                  <span className="num text-ink-3">{s.status}</span>
                </div>
              ))}
            </div>
          </section>
        ))}
      </div>
    </motion.aside>
  );
}

const PRESETS: { key: keyof typeof VIEW; label: string; icon: typeof Globe2 }[] = [
  { key: "WORLD", label: "World", icon: Globe2 }, { key: "INDIA", label: "India", icon: MapIcon }, { key: "HYDERABAD", label: "Hyderabad", icon: Building2 },
];

export function CameraPresets({ mapRef }: { mapRef: React.RefObject<MapRef | null> }) {
  const [active, setActive] = useState<keyof typeof VIEW>("INDIA");
  const fly = (k: keyof typeof VIEW) => {
    const v = VIEW[k];
    setActive(k);
    mapRef.current?.flyTo({ center: [v.longitude, v.latitude], zoom: v.zoom, pitch: v.pitch, bearing: v.bearing, duration: 2600, essential: true });
  };
  useEffect(() => {
    const onKey = (e: KeyboardEvent) => {
      if ((e.target as HTMLElement)?.tagName === "INPUT" || e.metaKey || e.ctrlKey) return;
      if (e.key === "w") fly("WORLD"); else if (e.key === "i") fly("INDIA"); else if (e.key === "h") fly("HYDERABAD");
    };
    window.addEventListener("keydown", onKey);
    return () => window.removeEventListener("keydown", onKey);
  }, []); // eslint-disable-line react-hooks/exhaustive-deps
  return (
    <Glass className="absolute left-4 top-[150px] z-10 p-1 flex gap-1" role="toolbar" aria-label="Camera presets">
      {PRESETS.map(({ key, label, icon: Icon }) => (
        <button key={key} onClick={() => fly(key)} title={`${label} (${key[0].toLowerCase()})`}
          className={clsx("h-8 px-2.5 rounded-lg flex items-center gap-1.5 text-[12px] cursor-pointer transition",
            active === key ? "bg-ok/15 text-ok" : "text-ink-2 hover:text-ink hover:bg-white/5")}>
          <Icon size={13} /> {label}
        </button>
      ))}
    </Glass>
  );
}

export function LiveStatusBar({ fps }: { fps: number }) {
  const { worldTs, frames, lastFrameMs, vehicleCount, kpis, status } = useLive();
  const [, force] = useState(0);
  useEffect(() => { const i = setInterval(() => force((x) => x + 1), 1000); return () => clearInterval(i); }, []);
  const age = lastFrameMs ? (Date.now() - lastFrameMs) / 1000 : Infinity;
  const stale = age > 3;
  const twinTs = useMemo(() => kpis?.twin?.ts?.slice(0, 16).replace("T", " "), [kpis]);
  return (
    <Glass className="absolute left-4 right-4 bottom-4 h-[76px] z-10 px-5 flex items-center gap-8">
      <div className="flex items-center gap-2.5">
        <Radio size={16} className={stale ? "text-warn" : "text-ok"} />
        <div>
          <Eyebrow>{status === "live" ? (stale ? "Live · waiting for data" : "Live") : "Reconnecting"}</Eyebrow>
          <div className="num text-[15px] text-ink mt-0.5">{worldTs ? worldTs.slice(0, 19).replace("T", " ") : "—"}</div>
        </div>
      </div>
      {[["Observed world", "telemetry clock"], ["Twin clock", twinTs ?? "—"], ["Vehicles", vehicleCount.toLocaleString("en-IN")], ["Frames", frames.toLocaleString("en-IN")],
        ["Last frame", Number.isFinite(age) ? `${age.toFixed(1)} s ago` : "—"], ["Render", `${fps} fps`]].slice(1).map(([k, v]) => (
        <div key={k}>
          <div className="eyebrow">{k}</div>
          <div className="num text-[13px] text-ink-2 mt-1">{v}</div>
        </div>
      ))}
      <div className="ml-auto flex items-center gap-2">
        <Button variant="ghost" onClick={() => window.open(`${(import.meta.env.VITE_API_URL as string | undefined) ?? "http://localhost:8000"}/docs`, "_blank")}>API docs</Button>
      </div>
    </Glass>
  );
}

/** Disruptions active in the live twin (pushed from the Scenario Lab or live events): what the map is showing in red. */
export function DisruptionBanner() {
  const { disruptions, network, refreshNetwork } = useLive();
  const shown = disruptions.filter((d) => d.kind !== "random_failure" && d.kind !== "hidden");
  if (!shown.length) return null;
  const name = (id: string) => network?.nodes.find((n) => n.id === id)?.name.split(" (")[0] ?? id;
  return (
    <div className="absolute left-1/2 -translate-x-1/2 top-[150px] z-20 flex flex-col gap-2 w-[440px]">
      {shown.map((d) => (
        <motion.div key={d.id} initial={{ opacity: 0, y: -8 }} animate={{ opacity: 1, y: 0 }}
          className="glass !bg-[#1a0c10]/92 border-bad/50 px-3.5 py-2.5 flex items-center gap-3">
          <Zap size={16} className="text-bad shrink-0" />
          <div className="min-w-0 flex-1">
            <div className="text-[12.5px] text-ink font-medium truncate">{d.label} · live twin</div>
            <div className="num text-[11px] text-ink-3 truncate">
              {d.type} @ {name(d.target)}{d.nodes.length ? ` · ${d.nodes.length} node(s) down` : ""}{d.lanes.length ? ` · ${d.lanes.length} lane(s) slowed` : ""}
              {d.end_h !== null ? ` · ends in ${Math.max(0, d.end_h - (useLive.getState().kpis?.twin?.t_h ?? d.start_h)).toFixed(1)} h` : ""}
            </div>
          </div>
          <Button variant="ghost" onClick={() => endDisruption(d.id).then(() => { toast.success("Disruption lifted"); void refreshNetwork(); })
            .catch((e) => toast.error("Could not end it", { description: String(e) }))}>End</Button>
        </motion.div>
      ))}
    </div>
  );
}
