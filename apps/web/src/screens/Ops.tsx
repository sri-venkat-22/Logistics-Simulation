import { useEffect, useMemo, useState } from "react";
import { toast } from "sonner";
import clsx from "clsx";
import { Skull, Server, Activity, Gauge as GaugeIcon, ShieldCheck } from "lucide-react";
import { Chart } from "../components/Chart";
import { Badge, Bar, Eyebrow, Glass, PanelHeader, Ticker } from "../components/ui";
import { ops } from "../lib/data";
import { C, axis, chartBase } from "../lib/theme";

type Pod = (typeof ops.pods)[number];

function panel(title: string, data: number[], color: string, unit: string, dipAt: number | null) {
  const series = dipAt === null ? data : data.map((v, i) => (i >= dipAt && i < dipAt + 4 ? Math.round(v * (i === dipAt ? 0.62 : i === dipAt + 1 ? 0.78 : 0.93)) : v));
  return {
    ...chartBase,
    grid: { left: 44, right: 12, top: 12, bottom: 20 },
    xAxis: { type: "category", data: ops.series.t.map((t) => `-${60 - t}m`), ...axis, axisLabel: { ...axis.axisLabel, interval: 14 } },
    yAxis: { type: "value", ...axis, scale: true },
    tooltip: { ...chartBase.tooltip, valueFormatter: (v: number) => `${v.toLocaleString("en-IN")} ${unit}` },
    series: [{ name: title, type: "line", data: series, showSymbol: false, smooth: 0.3, lineStyle: { color, width: 1.6 },
      areaStyle: { color: { type: "linear", x: 0, y: 0, x2: 0, y2: 1, colorStops: [{ offset: 0, color: `${color}40` }, { offset: 1, color: `${color}00` }] } } }],
  };
}

export default function Ops() {
  const [pods, setPods] = useState<Pod[]>(ops.pods);
  const [dipAt, setDipAt] = useState<number | null>(null);

  const kill = (name: string) => {
    const svc = pods.find((p) => p.name === name)!.svc;
    const replacement = `${name.split("-").slice(0, -1).join("-")}-${Math.random().toString(36).slice(2, 5)}`;
    setPods((ps) => ps.map((p) => (p.name === name ? { ...p, status: "Terminating", cpu: 0 } : p)));
    setDipAt(56);
    toast.error(`kubectl delete pod ${name}`, { description: "Chaos drill: watch Kubernetes restore it" });
    window.setTimeout(() => setPods((ps) => ps.map((p) => (p.name === name ? { ...p, name: replacement, status: "ContainerCreating", age: "0s", restarts: 0 } : p))), 1600);
    window.setTimeout(() => {
      setPods((ps) => ps.map((p) => (p.name === replacement ? { ...p, status: "Running", cpu: 42, mem: 30, age: "6s" } : p)));
      toast.success(`${svc} self-healed in 6 s`, { description: "ReplicaSet recreated the pod · readiness probe green · 0 dropped messages (Redis Streams consumer group)" });
    }, 5200);
  };
  useEffect(() => {
    const id = window.setTimeout(() => setPods((ps) => ps.map((p) => (p.status === "ContainerCreating" && p.svc === "sim-worker" ? { ...p, status: "Running", cpu: 88, mem: 51, age: "9s" } : p))), 2500);
    return () => window.clearTimeout(id);
  }, []);

  const ingest = useMemo(() => panel("Ingest", ops.series.ingest, C.ok, "msg/s", dipAt), [dipAt]);
  const p95 = useMemo(() => panel("p95", ops.series.p95, C.warn, "ms", null), []);
  const queue = useMemo(() => panel("Queue", ops.series.queue, C.ai, "jobs", null), []);
  const lt = ops.loadtest;
  const ltOption = useMemo(() => ({
    ...chartBase,
    grid: { left: 48, right: 48, top: 30, bottom: 30 },
    legend: { top: 0, right: 8, itemWidth: 12, itemHeight: 3, textStyle: { color: C.ink2, fontSize: 11 } },
    xAxis: { type: "category", data: lt.ramp.map((r) => r.vus), name: "virtual users", nameLocation: "middle", nameGap: 20, nameTextStyle: { color: C.ink3, fontSize: 10 }, ...axis },
    yAxis: [
      { type: "value", ...axis, name: "msg/s", nameTextStyle: { color: C.ink3, fontSize: 10 } },
      { type: "value", ...axis, name: "p95 ms", nameTextStyle: { color: C.ink3, fontSize: 10 }, splitLine: { show: false } },
    ],
    series: [
      { name: "Throughput", type: "bar", data: lt.ramp.map((r) => ({ value: r.msgs_s, itemStyle: { color: r.p95_ms < 1000 ? C.ok : C.bad } })), barWidth: "45%", itemStyle: { borderRadius: [3, 3, 0, 0] },
        markLine: { symbol: "none", data: [{ yAxis: lt.target_msgs_s }], lineStyle: { color: C.ink2, type: "dashed" }, label: { formatter: "NFR ≥ 5,000", color: C.ink2, fontSize: 10, position: "insideStartTop" } } },
      { name: "p95 latency", type: "line", yAxisIndex: 1, data: lt.ramp.map((r) => r.p95_ms), symbolSize: 6, lineStyle: { color: C.warn }, itemStyle: { color: C.warn },
        markLine: { symbol: "none", data: [{ yAxis: 1000 }], lineStyle: { color: C.warn, type: "dotted" }, label: { formatter: "p95 < 1 s", color: C.warn, fontSize: 10, position: "insideEndTop" } } },
    ],
  }), [lt]);

  const running = pods.filter((p) => p.status === "Running").length;

  return (
    <div className="absolute inset-0 pt-14 px-4 pb-4 overflow-y-auto scroll-thin">
      <div className="grid grid-cols-4 gap-3">
        {[
          [ShieldCheck, "Availability · 30 d", <><Ticker value={ops.slo.availability} decimals={2} />%</>, `SLO ${ops.slo.target}% · ${ops.slo.error_budget_left}% error budget left`],
          [Server, "Pods ready", <>{running}<span className="text-ink-3 text-[16px]"> / {pods.length}</span></>, "k3s · 1 node pool · HPA + KEDA"],
          [Activity, "Peak ingest (k6)", <><Ticker value={lt.peak_msgs_s} /></>, `msg/s on ${lt.vcpu} vCPU · p95 ${lt.p95_ms} ms`],
          [GaugeIcon, "Error rate", <><Ticker value={lt.error_rate * 100} decimals={2} />%</>, "under load test ramp"],
        ].map(([I, label, v, sub]) => {
          const Icon = I as typeof Server;
          return (
            <Glass key={label as string} className="px-4 py-3">
              <div className="flex items-center gap-2"><Icon size={13} className="text-ink-3" /><Eyebrow>{label as string}</Eyebrow></div>
              <div className="mt-1.5 text-[26px] font-semibold leading-none num">{v as React.ReactNode}</div>
              <div className="text-[11px] text-ink-3 mt-1.5">{sub as string}</div>
            </Glass>
          );
        })}
      </div>

      <div className="mt-3 grid grid-cols-3 gap-3">
        {[["Ingest rate", "msg/s · Prometheus", ingest], ["API latency p95", "ms", p95], ["Sim queue depth", "Redis Stream · KEDA scaler", queue]].map(([t, s, o]) => (
          <Glass key={t as string}><PanelHeader title={t as string} sub={s as string} right={<span className="text-[10px] text-ink-3 uppercase tracking-wider">Grafana</span>} /><Chart option={o as object} height={150} /></Glass>
        ))}
      </div>

      <div className="mt-3 grid grid-cols-[1.35fr_1fr] gap-3">
        <Glass className="overflow-hidden">
          <PanelHeader title="Pods · namespace aegis" sub="Kill a pod to run the self-healing drill" right={<Badge sev="ok">{running}/{pods.length} Running</Badge>} />
          <table className="w-full text-[12px]">
            <thead>
              <tr className="text-left text-ink-3 border-b border-line">
                {["Pod", "Service", "Status", "Restarts", "CPU", "Mem", "Age", ""].map((h) => <th key={h} className="font-medium px-4 py-2 text-[10.5px] uppercase tracking-wider">{h}</th>)}
              </tr>
            </thead>
            <tbody>
              {pods.map((p) => (
                <tr key={p.name} className="border-b border-line/60 hover:bg-white/3">
                  <td className="num px-4 py-1.5 text-ink whitespace-nowrap">{p.name}</td>
                  <td className="px-4 text-ink-2 whitespace-nowrap">{p.svc}</td>
                  <td className="px-4">
                    <span className={clsx("inline-flex items-center gap-1.5 text-[11.5px] whitespace-nowrap", p.status === "Running" ? "text-good" : p.status === "Terminating" ? "text-bad" : "text-warn")}>
                      <span className={clsx("w-1.5 h-1.5 rounded-full", p.status === "Running" ? "bg-good" : p.status === "Terminating" ? "bg-bad" : "bg-warn animate-pulse")} />{p.status}
                    </span>
                  </td>
                  <td className="num px-4 text-ink-2">{p.restarts}</td>
                  <td className="px-4 w-[90px]"><Bar value={p.cpu} max={100} color={p.cpu > 85 ? C.warn : C.ok} height={4} /></td>
                  <td className="px-4 w-[90px]"><Bar value={p.mem} max={100} color={C.ai} height={4} /></td>
                  <td className="num px-4 text-ink-3">{p.age}</td>
                  <td className="px-3 text-right">
                    {!["redis", "postgres"].includes(p.svc) && p.status === "Running" && (
                      <button onClick={() => kill(p.name)} className="text-ink-3 hover:text-bad cursor-pointer p-1" title={`kubectl delete pod ${p.name}`} aria-label={`Kill ${p.name}`}><Skull size={13} /></button>
                    )}
                  </td>
                </tr>
              ))}
            </tbody>
          </table>
        </Glass>
        <Glass>
          <PanelHeader title={`Load test · ${lt.tool}`} sub="Ingest ramp on 4 vCPU — find the ceiling at p95 < 1 s" right={<Badge sev="ok">{lt.peak_msgs_s.toLocaleString("en-IN")} msg/s</Badge>} />
          <Chart option={ltOption} height={300} />
        </Glass>
      </div>
    </div>
  );
}
