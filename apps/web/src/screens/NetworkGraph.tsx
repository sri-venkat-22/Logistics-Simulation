import { useEffect, useMemo, useRef, useState } from "react";
import ForceGraph3D from "react-force-graph-3d";
import { toast } from "sonner";
import clsx from "clsx";
import { Play, RotateCcw, GitFork } from "lucide-react";
import { Badge, Bar, Button, Eyebrow, Glass } from "../components/ui";
import { network as mockNetwork } from "../lib/data";
import { getCascade, getCriticality, type CriticalNode } from "../lib/api";
import { useLive } from "../lib/live";
import { C, nodeTypeLabel } from "../lib/theme";

type NodeState = "ok" | "risk" | "failed";
interface GNode { id: string; name: string; type: string; val: number; x?: number; y?: number; z?: number }
interface GLink { source: string | GNode; target: string | GNode; mode: string; id: string }

const idOf = (v: string | GNode) => (typeof v === "string" ? v : v.id);

/** Motter–Lai-style cascade on the lane graph: a node fails once ≥ 50% of its inbound lanes come from failed nodes. */
function cascadeWaves(start: string): string[][] {
  const network = mockNetwork;
  const inbound = new Map<string, string[]>();
  for (const l of network.lanes) inbound.set(l.to_id, [...(inbound.get(l.to_id) ?? []), l.from_id]);
  const failed = new Set([start]);
  const waves = [[start]];
  for (let k = 0; k < 8; k++) {
    const next: string[] = [];
    for (const n of network.nodes) {
      if (failed.has(n.id)) continue;
      const ins = inbound.get(n.id) ?? [];
      if (ins.length && ins.filter((x) => failed.has(x)).length / ins.length >= 0.5) next.push(n.id);
    }
    if (!next.length) break;
    next.forEach((n) => failed.add(n));
    waves.push(next);
  }
  return waves;
}

function useSize(ref: React.RefObject<HTMLDivElement | null>) {
  const [s, setS] = useState({ w: 800, h: 600 });
  useEffect(() => {
    if (!ref.current) return;
    const ro = new ResizeObserver(([e]) => setS({ w: e.contentRect.width, h: e.contentRect.height }));
    ro.observe(ref.current);
    return () => ro.disconnect();
  }, [ref]);
  return s;
}

export default function NetworkGraph() {
  const box = useRef<HTMLDivElement>(null);
  const { w, h } = useSize(box);
  const [start, setStart] = useState("SUP_SHENZHEN");
  const [states, setStates] = useState<Record<string, NodeState>>({});
  const [running, setRunning] = useState(false);
  const timers = useRef<number[]>([]);
  const fg = useRef<{ zoomToFit: (ms?: number, px?: number) => void } | undefined>(undefined);
  const fitted = useRef(false);
  const liveNet = useLive((st) => st.network);
  const [crit, setCrit] = useState<CriticalNode[] | null>(null);
  const [alpha, setAlpha] = useState(0.25);
  const [deadLanes, setDeadLanes] = useState<Set<string>>(new Set());
  const [unserved, setUnserved] = useState<number | null>(null);
  useEffect(() => {
    if (!liveNet) return;
    const t = window.setTimeout(() => getCriticality(alpha).then((r) => setCrit(r.nodes)).catch(() => undefined), 200);
    return () => window.clearTimeout(t);
  }, [liveNet, alpha]);
  const live = !!liveNet && !!crit;
  const critBy = useMemo(() => new Map((crit ?? []).map((c) => [c.node, c])), [crit]);
  const net = liveNet ?? mockNetwork;
  const nameOf = (id: string) => (net.nodes.find((n) => n.id === id)?.name ?? id).split(" (")[0];

  const data = useMemo(() => ({
    nodes: net.nodes.map<GNode>((n) => ({ id: n.id, name: n.name, type: n.type,
      val: 1.5 + (live ? (critBy.get(n.id)?.betweenness ?? 0) : ((n as { betweenness?: number }).betweenness ?? 0)) * 40 })),
    links: net.lanes.map<GLink>((l) => ({ source: l.from_id, target: l.to_id, mode: l.mode, id: l.id })),
  }), [net, live, critBy]);

  const ranking = useMemo(() => {
    if (live) {
      return crit!.slice(0, 12).map((c) => ({ id: c.node, name: c.name, type: c.type, ttr: c.ttr_days, score: c.spof_score,
        sub: `REI ${(c.rei ?? 0).toFixed(2)} · unserved ${(c.unserved_share * 100).toFixed(0)}% · cascade ${c.cascade_size}` }));
    }
    const maxB = Math.max(...mockNetwork.nodes.map((n) => n.betweenness));
    return mockNetwork.nodes
      .filter((n) => n.type !== "zone")
      .map((n) => ({ id: n.id, name: n.name, type: n.type, ttr: n.ttr_d, score: (n.betweenness / maxB) * 0.6 + ((n.ttr_d ?? 0) / 10) * 0.4, sub: "" }))
      .sort((a, b) => b.score - a.score)
      .slice(0, 10);
  }, [live, crit]);

  const animate = (waves: { nodes: string[]; lanes: string[] }[], done: () => void) => {
    timers.current.forEach(clearTimeout);
    setStates({}); setDeadLanes(new Set()); setRunning(true);
    waves.forEach((wave, i) => {
      timers.current.push(window.setTimeout(() => {
        setStates((s) => {
          const next = { ...s };
          wave.nodes.forEach((id) => { next[id] = "failed"; });
          net.lanes.forEach((l) => { if ((wave.nodes.includes(l.from_id) || wave.lanes.includes(l.id)) && next[l.to_id] !== "failed") next[l.to_id] = "risk"; });
          return next;
        });
        setDeadLanes((d) => new Set([...d, ...wave.lanes]));
        if (i === waves.length - 1) { setRunning(false); done(); }
      }, 900 * i));
    });
  };

  const run = () => {
    if (live) {
      getCascade(start, alpha).then((c) => {
        animate(c.steps, () => {
          setUnserved(c.unserved_share);
          toast.error(`Cascade from ${nameOf(start)}`, { description: `${c.failed_lanes.length} lanes overloaded over ${c.steps.length - 1} step(s) (Motter–Lai, α ${alpha}); ${(c.unserved_share * 100).toFixed(1)}% of zone demand unserved.` });
        });
      }).catch((e) => toast.error("Cascade failed", { description: String(e) }));
      return;
    }
    const waves = cascadeWaves(start);
    animate(waves.map((w) => ({ nodes: w, lanes: [] })), () => {
      const total = waves.flat().length;
      toast.error(`Cascade from ${nameOf(start)}`, { description: `${total} node${total > 1 ? "s" : ""} failed across ${waves.length} wave${waves.length > 1 ? "s" : ""}. Auto-failover reassigns affected zones.` });
    });
  };
  useEffect(() => () => timers.current.forEach(clearTimeout), []);

  const color = (n: GNode) => {
    const s = states[n.id];
    if (s === "failed") return C.bad;
    if (s === "risk") return C.warn;
    return n.type === "zone" ? "#5D6A82" : C.ok;
  };
  const failedCount = Object.values(states).filter((s) => s === "failed").length;

  return (
    <div className="absolute inset-0 pt-14 px-4 pb-4 flex gap-3">
      <div ref={box} className="relative flex-1 min-w-0 rounded-[14px] overflow-hidden border border-line bg-bg">
        <ForceGraph3D
          ref={fg as never}
          graphData={data}
          cooldownTicks={120}
          onEngineStop={() => { if (!fitted.current) { fitted.current = true; fg.current?.zoomToFit(600, 40); } }}
          width={w}
          height={h}
          backgroundColor="#070B14"
          nodeLabel={(n: GNode) => `<div style="font:12px Geist Variable,sans-serif;background:rgba(15,22,38,.95);padding:6px 9px;border-radius:8px;border:1px solid rgba(255,255,255,.12)"><b>${n.name}</b><br/><span style="color:#9AA7BD">${n.id}</span></div>`}
          nodeVal={(n: GNode) => n.val}
          nodeColor={color}
          nodeOpacity={0.95}
          nodeResolution={16}
          linkColor={(l: GLink) => (states[idOf(l.source)] === "failed" || deadLanes.has(l.id) ? C.bad : "rgba(154,167,189,0.35)")}
          linkWidth={(l: GLink) => (states[idOf(l.source)] === "failed" || deadLanes.has(l.id) ? 1.6 : 0.4)}
          linkDirectionalParticles={(l: GLink) => (states[idOf(l.source)] === "failed" || deadLanes.has(l.id) ? 0 : 2)}
          linkDirectionalParticleSpeed={0.006}
          linkDirectionalParticleWidth={1.4}
          linkDirectionalParticleColor={() => C.ok}
          onNodeClick={(n: GNode) => { if (n.type !== "zone") setStart(n.id); }}
          enableNodeDrag={false}
          showNavInfo={false}
        />
        <div className="absolute left-3 top-3 glass px-3 py-2 text-[11.5px] space-y-1 pointer-events-none">
          <div className="flex items-center gap-2"><span className="w-2.5 h-2.5 rounded-full bg-ok" /> Operating</div>
          <div className="flex items-center gap-2"><span className="w-2.5 h-2.5 rounded-full bg-warn" /> At risk (upstream failed)</div>
          <div className="flex items-center gap-2"><span className="w-2.5 h-2.5 rounded-full bg-bad" /> Failed</div>
          <div className="text-ink-3 pt-1">Size = betweenness centrality · drag to orbit</div>
        </div>
      </div>

      <div className="w-[340px] shrink-0 flex flex-col gap-3">
        <Glass className="p-4">
          <div className="flex items-center gap-2 text-[13px] font-semibold"><GitFork size={14} className="text-bad" /> Cascade simulation</div>
          <div className="text-[11.5px] text-ink-3 mt-1">{live
            ? "Motter–Lai load redistribution on the lane flows: the failed node's flows reroute onto the shortest surviving paths; lanes pushed past capacity (1 + α)·load + α·spare fail in turn."
            : "Click a node in the graph, or pick one below. A node fails when ≥ 50% of its inbound lanes come from failed nodes."}</div>
          <select value={start} onChange={(e) => setStart(e.target.value)} className="mt-3 w-full h-9 rounded-lg bg-black/30 border border-line-strong px-2.5 text-[12.5px] text-ink outline-none">
            {net.nodes.filter((n) => n.type !== "zone").map((n) => <option key={n.id} value={n.id}>{n.name}</option>)}
          </select>
          {live && (
            <label className="mt-3 flex items-center gap-2 text-[11px] text-ink-3">Tolerance α
              <input type="range" min={0} max={1} step={0.05} value={alpha} onChange={(e) => setAlpha(Number(e.target.value))} className="flex-1" aria-label="Cascade tolerance alpha" />
              <span className="num w-8 text-right text-ink-2">{alpha.toFixed(2)}</span>
            </label>
          )}
          <div className="mt-3 flex gap-2">
            <Button variant="danger" className="flex-1" onClick={run} disabled={running}><Play size={13} /> Fail {nameOf(start)}</Button>
            <Button onClick={() => { timers.current.forEach(clearTimeout); setStates({}); setDeadLanes(new Set()); setUnserved(null); setRunning(false); }} aria-label="Reset"><RotateCcw size={13} /></Button>
          </div>
          {(failedCount > 0 || deadLanes.size > 0) && <div className="mt-3 text-[12px] text-ink-2"><span className="num text-bad">{failedCount}</span> nodes failed · <span className="num text-bad">{deadLanes.size}</span> lanes down · <span className="num text-warn">{Object.values(states).filter((s) => s === "risk").length}</span> at risk
            {unserved !== null && <> · <span className="num text-bad">{(unserved * 100).toFixed(1)}%</span> demand unserved</>}</div>}
        </Glass>
        <Glass className="flex-1 flex flex-col overflow-hidden">
          <div className="px-4 pt-3.5 pb-1 flex items-center justify-between">
            <div className="text-[13px] font-semibold">Single points of failure</div>
            <Badge sev="warn">{live ? "SPOF" : "REI"}</Badge>
          </div>
          <div className="px-4 text-[11px] text-ink-3">{live ? "0.4 REI (Simchi-Levi) + 0.3 cascade unserved demand + 0.2 flow share + 0.1 betweenness" : "Risk Exposure Index = betweenness × time-to-recover"}</div>
          <div className="flex-1 overflow-y-auto scroll-thin p-2 mt-1">
            {ranking.map((r, i) => (
              <button key={r.id} onClick={() => setStart(r.id)}
                className={clsx("w-full text-left grid grid-cols-[20px_1fr_70px] items-center gap-2 px-2 py-2 rounded-lg cursor-pointer transition", start === r.id ? "bg-white/6" : "hover:bg-white/4")}>
                <span className="num text-[11px] text-ink-3">{i + 1}</span>
                <span className="min-w-0">
                  <span className="block text-[12px] text-ink truncate">{r.name.split(" (")[0]}</span>
                  <span className="block text-[10.5px] text-ink-3">{nodeTypeLabel[r.type as keyof typeof nodeTypeLabel] ?? r.type} · TTR {r.ttr ?? "–"} d{r.sub ? ` · ${r.sub}` : ""}</span>
                </span>
                <span><Bar value={r.score} max={1} color={r.score > 0.6 ? C.bad : r.score > 0.35 ? C.warn : C.ok} /><span className="num block text-right text-[10.5px] text-ink-2 mt-1">{r.score.toFixed(2)}</span></span>
              </button>
            ))}
          </div>
          <div className="px-4 py-2.5 border-t border-line"><Eyebrow>{net.nodes.length} nodes · {net.lanes.length} lanes · {live ? "live API · NetworkX" : "prototype data"}</Eyebrow></div>
        </Glass>
      </div>
    </div>
  );
}
