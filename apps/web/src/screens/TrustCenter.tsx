import { useEffect, useMemo, useRef, useState } from "react";
import { AnimatePresence, motion } from "motion/react";
import { toast } from "sonner";
import clsx from "clsx";
import { PathLayer, ScatterplotLayer, TextLayer } from "@deck.gl/layers";
import { PathStyleExtension } from "@deck.gl/extensions";
import { MapPinOff, MoveDiagonal, PackageX, Repeat, FileWarning, WifiOff, GitFork, ShieldCheck, Zap, type LucideIcon } from "lucide-react";
import { DeckMap, useAnimationClock } from "../components/DeckMap";
import { Chart } from "../components/Chart";
import { Badge, Bar, Eyebrow, Glass, Ticker } from "../components/ui";
import { trust, type QEntry } from "../lib/data";
import { rng } from "../lib/geo";
import { useAegis } from "../lib/store";
import { C, axis, chartBase } from "../lib/theme";

const ICONS: Record<string, LucideIcon> = { "map-pin-off": MapPinOff, "move-diagonal": MoveDiagonal, "package-x": PackageX, repeat: Repeat, "file-warning": FileWarning, "wifi-off": WifiOff, "git-fork": GitFork };

const ATTACK_LOG: Record<string, Omit<QEntry, "id" | "t">[]> = {
  gps_teleport: [{ source: "gps-fleet-hyd", code: "PHYSICS_TELEPORT", layer: "Physics", detail: "R4471 jumped 612 km in 4 s (Hyderabad → Mumbai)" }],
  drift: [{ source: "gps-fleet-south", code: "KALMAN_GATE", layer: "Kalman", detail: "R2208 innovation d² = 38.2 > χ²₀.₉₉ (slow drift)" }],
  asn_inflate: [{ source: "asn-sup-chennai-auto", code: "ASN_OUTLIER", layer: "Feed anomaly", detail: "ASN claims 1,000,000 units (MAD z = 41.0)" }],
  replay: [{ source: "gps-fleet-west", code: "REPLAY_NONCE", layer: "Temporal", detail: "nonce 9f2c… reused within 60 s" }],
  malformed: [
    { source: "wms-medchal", code: "SCHEMA_NAN_QTY", layer: "Schema", detail: "on_hand = NaN" },
    { source: "gps-fleet-hyd", code: "SCHEMA_MISSING", layer: "Schema", detail: "lat missing → imputed from twin" },
    { source: "wms-blr", code: "NEG_QTY", layer: "Schema", detail: "on_hand = -340" },
  ],
  blackout: [{ source: "gps-fleet-south", code: "SLA_SILENT", layer: "Reputation", detail: "30% of sources silent > 30 s → PREDICTED mode" }],
  cascade: [{ source: "twin", code: "NODE_FAILED", layer: "Twin oracle", detail: "DC_NAGPUR down → 3 zones failed over to DC_HYD_MEDCHAL" }],
};

const HYD: [number, number] = [78.47, 17.6];

interface Veh { id: string; base: [number, number]; ang: number; r: number }
const fleet: Veh[] = (() => {
  const r = rng(5);
  return Array.from({ length: 140 }, (_, i) => ({ id: `R${4000 + i}`, base: [78.1 + r() * 0.8, 17.1 + r() * 0.8] as [number, number], ang: r() * Math.PI * 2, r: 0.02 + r() * 0.05 }));
})();

function stamp() {
  const d = new Date();
  return d.toLocaleTimeString("en-IN", { hour12: false, timeZone: "Asia/Kolkata" });
}

export default function TrustCenter() {
  const t = useAnimationClock();
  const { quarantine, pushQuarantine, activeAttacks, triggerAttack, clearAttack, detected } = useAegis();
  const [blackoutAt, setBlackoutAt] = useState<number | null>(null);
  const [trustScores, setTrustScores] = useState(() => Object.fromEntries(trust.sources.map((s) => [s.id, s.trust])));
  const seq = useRef(88200);
  const [timeline, setTimeline] = useState(trust.timeline);

  // background "live" noise so the log never looks frozen
  useEffect(() => {
    const r = rng(3);
    const pool = trust.quarantine;
    const id = window.setInterval(() => {
      const q = pool[Math.floor(r() * pool.length)];
      pushQuarantine({ ...q, id: `Q-${seq.current++}`, t: stamp() });
    }, 3200);
    return () => window.clearInterval(id);
  }, [pushQuarantine]);

  const inject = (id: string, label: string) => {
    triggerAttack(id);
    (ATTACK_LOG[id] ?? []).forEach((e, i) => window.setTimeout(() => pushQuarantine({ ...e, id: `Q-${seq.current++}`, t: stamp() }), 350 + i * 250));
    setTimeline((tl) => tl.map((b, i) => (i === tl.length - 1 ? { ...b, rejected: b.rejected + (id === "malformed" ? 1400 : 60) } : b)));
    if (id === "blackout") setBlackoutAt(performance.now());
    if (id === "asn_inflate") setTrustScores((s) => ({ ...s, "asn-sup-chennai-auto": 0.18 }));
    if (id === "gps_teleport") setTrustScores((s) => ({ ...s, "gps-fleet-hyd": 0.58 }));
    toast.error(`${label} injected`, { description: id === "blackout" ? "Twin switches to predictive dead-reckoning — cones widen, nothing crashes." : "Caught by the trust pipeline in 1.4 s · quarantined" });
    window.setTimeout(() => clearAttack(id), id === "blackout" ? 20000 : 12000);
  };

  const teleport = activeAttacks.includes("gps_teleport");
  const drift = activeAttacks.includes("drift");
  const blackout = activeAttacks.includes("blackout");
  const sinceBlackout = blackoutAt ? (performance.now() - blackoutAt) / 1000 : 0;

  const layers = [
    new ScatterplotLayer<Veh>({
      id: "fleet",
      data: fleet,
      getPosition: (v) => [v.base[0] + Math.cos(v.ang + t * 0.2) * v.r, v.base[1] + Math.sin(v.ang + t * 0.2) * v.r],
      getRadius: 3, radiusUnits: "pixels",
      getFillColor: (v) => (blackout && Number(v.id.slice(1)) % 10 < 3 ? [154, 167, 189, 110] : [34, 211, 238, 220]),
      updateTriggers: { getPosition: t, getFillColor: blackout },
    }),
    ...(blackout ? [new ScatterplotLayer<Veh>({
      id: "cones",
      data: fleet.filter((v) => Number(v.id.slice(1)) % 10 < 3),
      getPosition: (v) => [v.base[0] + Math.cos(v.ang + t * 0.2) * v.r, v.base[1] + Math.sin(v.ang + t * 0.2) * v.r],
      getRadius: () => 800 + sinceBlackout * 380,
      getFillColor: [245, 158, 11, 18], getLineColor: [245, 158, 11, 120], stroked: true, lineWidthMinPixels: 1,
      updateTriggers: { getPosition: t, getRadius: t },
    })] : []),
    ...(teleport || drift ? [
      new PathLayer({
        id: "spoof-link",
        data: [{ path: (teleport ? [HYD, [72.88, 19.07]] : [HYD, [HYD[0] + 0.35, HYD[1] - 0.2]]) as [number, number][] }],
        getPath: (d: { path: [number, number][] }) => d.path,
        getColor: [239, 68, 68, 200], getWidth: 2, widthUnits: "pixels",
        ...({ getDashArray: [4, 3], dashJustified: true } as object),
        extensions: [new PathStyleExtension({ dash: true })],
      }),
      new ScatterplotLayer({
        id: "ghost",
        data: [{ p: teleport ? [72.88, 19.07] : [HYD[0] + 0.35, HYD[1] - 0.2] }],
        getPosition: (d: { p: [number, number] }) => d.p,
        getRadius: 9 + 4 * Math.sin(t * 5), radiusUnits: "pixels",
        getFillColor: [239, 68, 68, 90], getLineColor: [239, 68, 68, 255], stroked: true, lineWidthMinPixels: 2,
        updateTriggers: { getRadius: t },
      }),
      new ScatterplotLayer({
        id: "kalman",
        data: [{ p: HYD }],
        getPosition: (d: { p: [number, number] }) => d.p,
        getRadius: 8, radiusUnits: "pixels",
        getFillColor: [52, 211, 153, 255], getLineColor: [7, 11, 20, 255], stroked: true, lineWidthMinPixels: 2,
      }),
      new TextLayer({
        id: "spoof-labels",
        data: [
          { p: teleport ? [72.88, 19.07] : [HYD[0] + 0.35, HYD[1] - 0.2], text: "R4471 reported (spoofed)", c: [239, 68, 68, 255] },
          { p: HYD, text: "Kalman estimate (true)", c: [52, 211, 153, 255] },
        ],
        getPosition: (d: { p: [number, number] }) => d.p, getText: (d: { text: string }) => d.text,
        getColor: (d: { c: [number, number, number, number] }) => d.c, getSize: 12, getPixelOffset: [0, -20],
        fontFamily: "Geist Variable, Inter, sans-serif", fontWeight: 600, outlineWidth: 3, outlineColor: [7, 11, 20, 255], fontSettings: { sdf: true },
      }),
    ] : []),
  ];

  const timelineOption = useMemo(() => ({
    ...chartBase,
    grid: { left: 44, right: 44, top: 26, bottom: 22 },
    legend: { top: 0, right: 8, itemWidth: 10, itemHeight: 8, textStyle: { color: C.ink2, fontSize: 11 } },
    xAxis: { type: "category", data: timeline.map((b) => `-${60 - b.m}m`), ...axis, axisLabel: { ...axis.axisLabel, interval: 9 } },
    yAxis: [
      { type: "value", ...axis, name: "clean msg/s", nameTextStyle: { color: C.ink3, fontSize: 10 } },
      { type: "value", ...axis, name: "rejected", nameTextStyle: { color: C.ink3, fontSize: 10 }, splitLine: { show: false } },
    ],
    series: [
      { name: "Clean", type: "line", data: timeline.map((b) => Math.round(b.clean / 60 * 60)), showSymbol: false, lineStyle: { color: C.ok, width: 1.5 }, areaStyle: { color: "rgba(34,211,238,0.08)" }, itemStyle: { color: C.ok } },
      { name: "Rejected / quarantined", type: "bar", yAxisIndex: 1, data: timeline.map((b) => b.rejected), itemStyle: { color: C.bad, borderRadius: [2, 2, 0, 0] }, barWidth: "60%" },
    ],
  }), [timeline]);

  const counters = [
    ["Messages · 24 h", trust.counters.msgs_24h, "", "ok"],
    ["Quarantined", trust.counters.quarantined_24h + (quarantine.length - 14), "", "warn"],
    ["Attacks detected", detected, "", "bad"],
    ["Mean time-to-detect", trust.counters.mttd_s, " s", "ok"],
    ["Crashes", 0, "", "good"],
  ] as const;

  return (
    <div className="absolute inset-0 pt-14 px-4 pb-4 grid grid-cols-[292px_1fr_356px] grid-rows-[76px_1fr_190px] gap-3">
      <div className="col-span-3 grid grid-cols-5 gap-3">
        {counters.map(([label, v, suf, sev]) => (
          <Glass key={label} className="px-4 py-2.5">
            <Eyebrow>{label}</Eyebrow>
            <div className="mt-1 text-[22px] font-semibold" style={{ color: label === "Crashes" ? C.good : C.ink }}>
              <Ticker value={v} decimals={label === "Mean time-to-detect" ? 1 : 0} />{suf}
              {label === "Crashes" && <span className="ml-2 text-[11px] text-ink-3 font-normal">under 30% hostile input</span>}
              {sev === "bad" && <span className="ml-2 text-[11px] text-ink-3 font-normal">labelled benchmark</span>}
            </div>
          </Glass>
        ))}
      </div>

      <Glass className="row-span-2 flex flex-col overflow-hidden">
        <div className="px-4 pt-3.5 flex items-center justify-between">
          <div className="text-[13px] font-semibold flex items-center gap-2"><Zap size={14} className="text-bad" /> Chaos Console</div>
          <Badge sev="bad">security role</Badge>
        </div>
        <div className="grid grid-cols-2 gap-2 p-3">
          {trust.chaos.map((c) => {
            const I = ICONS[c.icon] ?? Zap;
            const on = activeAttacks.includes(c.id);
            return (
              <button key={c.id} onClick={() => inject(c.id, c.label)}
                className={clsx("text-left rounded-xl border p-2.5 transition cursor-pointer", on ? "border-bad/60 bg-bad/10" : "border-line hover:border-bad/40 hover:bg-bad/5")}>
                <I size={15} className={on ? "text-bad" : "text-ink-2"} />
                <div className="mt-1.5 text-[12px] font-medium text-ink leading-tight">{c.label}</div>
                <div className="text-[10.5px] text-ink-3 leading-tight mt-0.5">{c.desc}</div>
              </button>
            );
          })}
        </div>
        <div className="px-4 pt-1 pb-2 border-t border-line mt-1">
          <div className="flex items-center justify-between pt-3"><Eyebrow>Trust pipeline · rejects / 24 h</Eyebrow><ShieldCheck size={13} className="text-good" /></div>
        </div>
        <div className="flex-1 overflow-y-auto scroll-thin px-4 pb-3 space-y-2">
          {trust.layers.map((l, i) => (
            <div key={l.name} className="grid grid-cols-[18px_88px_1fr_44px] items-center gap-2 text-[11.5px]">
              <span className="num text-ink-3">{i + 1}</span>
              <span className="text-ink-2 truncate">{l.name}</span>
              <Bar value={l.rejected} max={1900} color={C.bad} />
              <span className="num text-right text-ink">{l.rejected.toLocaleString("en-IN")}</span>
            </div>
          ))}
        </div>
      </Glass>

      <div className="relative rounded-[14px] overflow-hidden border border-line">
        <DeckMap initialViewState={{ longitude: 76.2, latitude: 18.4, zoom: 5.6, pitch: 0, bearing: 0 }} layers={layers} />
        <div className="absolute left-3 top-3 glass px-3 py-2 text-[11.5px] space-y-1">
          <div className="flex items-center gap-2"><span className="w-2.5 h-2.5 rounded-full border-2 border-bad bg-bad/30" /> Reported position (quarantined)</div>
          <div className="flex items-center gap-2"><span className="w-2.5 h-2.5 rounded-full bg-good" /> Kalman estimate</div>
          <div className="flex items-center gap-2"><span className="w-2.5 h-2.5 rounded-full border border-warn bg-warn/20" /> PREDICTED · uncertainty cone</div>
        </div>
        {!teleport && !drift && !blackout && (
          <div className="absolute left-1/2 bottom-4 -translate-x-1/2 glass px-3.5 py-2 text-[12px] text-ink-2">Inject an attack from the Chaos Console to see detection live.</div>
        )}
      </div>

      <Glass className="row-span-2 flex flex-col overflow-hidden">
        <div className="px-4 pt-3.5 pb-2 text-[13px] font-semibold">Source trust</div>
        <div className="px-4 space-y-2 max-h-[250px] overflow-y-auto scroll-thin">
          {trust.sources.map((s) => {
            const v = trustScores[s.id];
            const sev = v < 0.4 ? "bad" : v < 0.7 ? "warn" : "ok";
            return (
              <div key={s.id} className="grid grid-cols-[1fr_80px_36px] items-center gap-2">
                <div className="min-w-0"><div className="num text-[11.5px] text-ink truncate">{s.id}</div></div>
                <Bar value={v} color={sev === "bad" ? C.bad : sev === "warn" ? C.warn : C.ok} />
                <span className="num text-[11.5px] text-right" style={{ color: sev === "ok" ? C.ink : sev === "warn" ? C.warn : C.bad }}>{v.toFixed(2)}</span>
              </div>
            );
          })}
        </div>
        <div className="px-4 pt-4 pb-2 flex items-center justify-between border-t border-line mt-3">
          <div className="text-[13px] font-semibold">Quarantine log</div>
          <span className="flex items-center gap-1.5 text-[11px] text-ink-3"><span className="w-1.5 h-1.5 rounded-full bg-bad pulse-dot text-bad" /> live</span>
        </div>
        <div className="flex-1 overflow-y-auto scroll-thin px-2 pb-2">
          <AnimatePresence initial={false}>
            {quarantine.map((q) => (
              <motion.div key={q.id} layout initial={{ opacity: 0, x: 16, backgroundColor: "rgba(239,68,68,0.12)" }} animate={{ opacity: 1, x: 0, backgroundColor: "rgba(239,68,68,0)" }} transition={{ duration: 0.6 }}
                className="rounded-lg px-2.5 py-2">
                <div className="flex items-center gap-2">
                  <span className="num text-[10.5px] text-ink-3">{q.t}</span>
                  <span className="num text-[10.5px] font-semibold text-bad">{q.code}</span>
                  <span className="ml-auto text-[10px] text-ink-3">L{trust.layers.findIndex((l) => l.name === q.layer) + 1 || "–"} {q.layer}</span>
                </div>
                <div className="text-[11.5px] text-ink-2 mt-0.5 truncate">{q.detail}</div>
                <div className="num text-[10px] text-ink-3">{q.source}</div>
              </motion.div>
            ))}
          </AnimatePresence>
        </div>
      </Glass>

      <Glass className="flex flex-col min-w-0">
        <div className="px-4 pt-3 text-[13px] font-semibold">Attack timeline · last 60 min</div>
        <div className="flex-1 min-h-0"><Chart option={timelineOption} height="100%" /></div>
      </Glass>
    </div>
  );
}
