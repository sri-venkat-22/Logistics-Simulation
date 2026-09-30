import { useEffect, useMemo, useRef, useState } from "react";
import ForceGraph3D from "react-force-graph-3d";
import { toast } from "sonner";
import clsx from "clsx";
import { Play, RotateCcw, GitFork } from "lucide-react";
import { Badge, Bar, Button, Eyebrow, Glass } from "../components/ui";
import { network, nodeById } from "../lib/data";
import { C, nodeTypeLabel } from "../lib/theme";

type NodeState = "ok" | "risk" | "failed";
interface GNode { id: string; name: string; type: string; val: number; x?: number; y?: number; z?: number }
interface GLink { source: string | GNode; target: string | GNode; mode: string }

const idOf = (v: string | GNode) => (typeof v === "string" ? v : v.id);

/** Motter–Lai-style cascade on the lane graph: a node fails once ≥ 50% of its inbound lanes come from failed nodes. */
function cascadeWaves(start: string): string[][] {
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

  const data = useMemo(() => ({
    nodes: network.nodes.map<GNode>((n) => ({ id: n.id, name: n.name, type: n.type, val: 1.5 + n.betweenness * 40 })),
    links: network.lanes.map<GLink>((l) => ({ source: l.from_id, target: l.to_id, mode: l.mode })),
  }), []);

  const ranking = useMemo(() => {
    const maxB = Math.max(...network.nodes.map((n) => n.betweenness));
    return network.nodes
      .filter((n) => n.type !== "zone")
      .map((n) => ({ n, rei: (n.betweenness / maxB) * 0.6 + ((n.ttr_d ?? 0) / 10) * 0.4 }))
      .sort((a, b) => b.rei - a.rei)
      .slice(0, 10);
  }, []);

  const run = () => {
    timers.current.forEach(clearTimeout);
    const waves = cascadeWaves(start);
    setStates({});
    setRunning(true);
    waves.forEach((wave, i) => {
      timers.current.push(window.setTimeout(() => {
        setStates((s) => {
          const next = { ...s };
          wave.forEach((id) => { next[id] = "failed"; });
          // neighbours of the newly failed nodes are at risk
          network.lanes.forEach((l) => { if (wave.includes(l.from_id) && next[l.to_id] !== "failed") next[l.to_id] = "risk"; });
          return next;
        });
        if (i === waves.length - 1) {
          setRunning(false);
          const total = waves.flat().length;
          toast.error(`Cascade from ${nodeById.get(start)!.name.split(" (")[0]}`, { description: `${total} node${total > 1 ? "s" : ""} failed across ${waves.length} wave${waves.length > 1 ? "s" : ""}. Auto-failover reassigns affected zones.` });
        }
      }, 900 * i));
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
          linkColor={(l: GLink) => (states[idOf(l.source)] === "failed" ? C.bad : "rgba(154,167,189,0.35)")}
          linkWidth={(l: GLink) => (states[idOf(l.source)] === "failed" ? 1.6 : 0.4)}
          linkDirectionalParticles={(l: GLink) => (states[idOf(l.source)] === "failed" ? 0 : 2)}
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
          <div className="text-[11.5px] text-ink-3 mt-1">Click a node in the graph, or pick one below. A node fails when ≥ 50% of its inbound lanes come from failed nodes.</div>
          <select value={start} onChange={(e) => setStart(e.target.value)} className="mt-3 w-full h-9 rounded-lg bg-black/30 border border-line-strong px-2.5 text-[12.5px] text-ink outline-none">
            {network.nodes.filter((n) => n.type !== "zone").map((n) => <option key={n.id} value={n.id}>{n.name}</option>)}
          </select>
          <div className="mt-3 flex gap-2">
            <Button variant="danger" className="flex-1" onClick={run} disabled={running}><Play size={13} /> Fail {nodeById.get(start)!.name.split(" (")[0]}</Button>
            <Button onClick={() => { timers.current.forEach(clearTimeout); setStates({}); setRunning(false); }} aria-label="Reset"><RotateCcw size={13} /></Button>
          </div>
          {failedCount > 0 && <div className="mt-3 text-[12px] text-ink-2"><span className="num text-bad">{failedCount}</span> failed · <span className="num text-warn">{Object.values(states).filter((s) => s === "risk").length}</span> at risk</div>}
        </Glass>
        <Glass className="flex-1 flex flex-col overflow-hidden">
          <div className="px-4 pt-3.5 pb-1 flex items-center justify-between">
            <div className="text-[13px] font-semibold">Single points of failure</div>
            <Badge sev="warn">REI</Badge>
          </div>
          <div className="px-4 text-[11px] text-ink-3">Risk Exposure Index = betweenness × time-to-recover</div>
          <div className="flex-1 overflow-y-auto scroll-thin p-2 mt-1">
            {ranking.map(({ n, rei }, i) => (
              <button key={n.id} onClick={() => setStart(n.id)}
                className={clsx("w-full text-left grid grid-cols-[20px_1fr_70px] items-center gap-2 px-2 py-2 rounded-lg cursor-pointer transition", start === n.id ? "bg-white/6" : "hover:bg-white/4")}>
                <span className="num text-[11px] text-ink-3">{i + 1}</span>
                <span className="min-w-0">
                  <span className="block text-[12px] text-ink truncate">{n.name.split(" (")[0]}</span>
                  <span className="block text-[10.5px] text-ink-3">{nodeTypeLabel[n.type]} · TTR {n.ttr_d ?? "–"} d</span>
                </span>
                <span><Bar value={rei} max={1} color={rei > 0.6 ? C.bad : rei > 0.35 ? C.warn : C.ok} /><span className="num block text-right text-[10.5px] text-ink-2 mt-1">{rei.toFixed(2)}</span></span>
              </button>
            ))}
          </div>
          <div className="px-4 py-2.5 border-t border-line"><Eyebrow>{network.nodes.length} nodes · {network.lanes.length} lanes · NetworkX</Eyebrow></div>
        </Glass>
      </div>
    </div>
  );
}
