/**
 * Trust Center on the live API (Phases 7.5 + 8): the 9 trust layers with live reject counts, the red-team benchmark
 * score of the whole pipeline, twin divergences (L7), lowest-reputation sources (L9), device-key rotation and the
 * audit log (security role).
 */
import { useEffect, useState } from "react";
import { toast } from "sonner";
import clsx from "clsx";
import { KeyRound, RefreshCw, ScrollText, ShieldAlert, ShieldCheck } from "lucide-react";
import { Badge, Bar, Button, Eyebrow, Glass, PanelHeader } from "../components/ui";
import { getAudit, getBenchmark, getTrustLayers, rotateDeviceKey, type AuditRow, type Benchmark, type Divergence, type TrustLayer } from "../lib/api";
import { ensureRole, hasRole, useAuth } from "../lib/auth";
import { C } from "../lib/theme";

const TYPE_LABEL: Record<string, string> = {
  gps_teleport: "GPS teleport", gps_drift: "GPS drift", replay: "Replay", duplicate: "Duplicate", missing_fields: "Missing fields",
  nan_negative: "NaN / negative", inflated_asn: "Inflated ASN", stale_timestamp: "Stale timestamp", blackout: "Blackout",
  cascade_failure: "Cascade failure",
};

export default function TrustCenterLive() {
  const [layers, setLayers] = useState<TrustLayer[]>([]);
  const [divs, setDivs] = useState<Divergence[]>([]);
  const [rep, setRep] = useState<{ source: string; trust: number; good: number; bad: number }[]>([]);
  const [bench, setBench] = useState<Benchmark | null>(null);
  const [audit, setAudit] = useState<AuditRow[] | null>(null);
  const [src, setSrc] = useState("gps:TRK-SH000221");
  const [rotated, setRotated] = useState<{ source_id: string; version: number; key: string; previous_key_valid_until: string | null } | null>(null);
  const session = useAuth((s) => s.session);
  const canSec = hasRole("security");

  useEffect(() => {
    const load = () => getTrustLayers().then((r) => { setLayers(r.layers); setDivs(r.divergences); setRep(r.reputation); }).catch(() => undefined);
    load();
    getBenchmark().then(setBench).catch(() => undefined);
    const i = window.setInterval(load, 3000);
    return () => window.clearInterval(i);
  }, []);
  useEffect(() => {
    if (!canSec) { setAudit(null); return; }
    const load = () => getAudit(60).then(setAudit).catch(() => setAudit(null));
    load();
    const i = window.setInterval(load, 5000);
    return () => window.clearInterval(i);
  }, [canSec, session]);

  const maxRej = Math.max(1, ...layers.map((l) => l.rejected));
  const rotate = () => {
    if (!ensureRole("security")) return;
    rotateDeviceKey(src, 300).then((r) => { setRotated(r); toast.success(`Key rotated · ${r.source_id}`, { description: `version ${r.version}; old key valid until ${r.previous_key_valid_until?.slice(11, 19)}` }); })
      .catch((e) => toast.error("Rotation failed", { description: String(e) }));
  };

  return (
    <div className="absolute inset-0 pt-14 px-4 pb-4 grid grid-cols-[1.15fr_1fr_0.9fr] gap-3 min-h-0">
      <Glass className="flex flex-col min-h-0">
        <PanelHeader title="Trust pipeline · 9 layers" sub="Live quarantine counts since the API started" right={<Badge sev="good"><ShieldCheck size={11} /> live</Badge>} />
        <div className="flex-1 overflow-y-auto scroll-thin px-4 pb-3 space-y-2">
          {layers.map((l) => (
            <div key={l.id} className="rounded-lg border border-line px-3 py-2">
              <div className="flex items-center gap-2">
                <span className="num text-[11px] text-ok w-6">{l.id}</span>
                <span className="text-[12.5px] text-ink">{l.name}</span>
                <span className="num ml-auto text-[12px] text-ink">{l.rejected.toLocaleString("en-IN")}</span>
              </div>
              <div className="text-[10.5px] text-ink-3 mt-0.5">{l.technique}</div>
              <div className="mt-1.5"><Bar value={l.rejected} max={maxRej} color={l.rejected ? C.bad : C.ok} height={4} /></div>
              {Object.keys(l.by_code).length > 0 && (
                <div className="num mt-1 text-[10px] text-ink-3">{Object.entries(l.by_code).map(([k, v]) => `${k} ${v}`).join(" · ")}</div>
              )}
            </div>
          ))}
        </div>
      </Glass>

      <Glass className="flex flex-col min-h-0">
        <PanelHeader title="Red-team benchmark" sub={bench ? `${bench.messages.toLocaleString("en-IN")} labelled messages · ${bench.attack_level.attacks} attacks · ${bench.throughput_msgs_s.toLocaleString("en-IN")} msgs/s` : "python -m services.api.app.trust_bench"} />
        {bench && (
          <div className="px-4 grid grid-cols-4 gap-2 text-center">
            {[["Attacks caught", `${(bench.attack_level.recall * 100).toFixed(1)}%`], ["Precision", bench.message_level.precision.toFixed(3)],
              ["F1 (msgs)", bench.message_level.f1.toFixed(3)], ["False pos.", `${(bench.message_level.false_positive_rate * 100).toFixed(2)}%`]].map(([k, v]) => (
              <div key={k} className="rounded-lg bg-black/25 py-2"><div className="eyebrow !text-[9px]">{k}</div><div className="num text-[15px] text-ink">{v}</div></div>
            ))}
          </div>
        )}
        <div className="flex-1 overflow-y-auto scroll-thin px-4 py-2">
          {bench && (
            <table className="w-full text-[11.5px]">
              <thead><tr className="text-ink-3 text-left"><th className="py-1 font-normal">Attack</th><th className="font-normal text-right">caught</th><th className="font-normal text-right">TTD</th><th className="font-normal pl-2">layer</th></tr></thead>
              <tbody>
                {Object.entries(bench.by_type).map(([t, r]) => (
                  <tr key={t} className="border-t border-line/60">
                    <td className="py-1.5 text-ink-2">{TYPE_LABEL[t] ?? t}</td>
                    <td className={clsx("num text-right", r.attack_recall === 1 ? "text-good" : r.attack_recall > 0.8 ? "text-warn" : "text-bad")}>{r.detected}/{r.attacks}</td>
                    <td className="num text-right text-ink-3">{r.ttd_p50_s === null ? "—" : `${Math.round(r.ttd_p50_s)} s`}</td>
                    <td className="num pl-2 text-[10px] text-ink-3 truncate max-w-[120px]">{Object.keys(r.caught_by)[0] ?? (t === "blackout" ? "L9:SLA_SILENT" : t === "cascade_failure" ? "L7:TWIN_ENVELOPE" : "")}</td>
                  </tr>
                ))}
              </tbody>
            </table>
          )}
          <div className="mt-3"><Eyebrow>Twin divergences (L7)</Eyebrow></div>
          {divs.length === 0 ? <div className="text-[11.5px] text-ink-3 mt-1">Reality is inside the twin's envelope.</div> : (
            <ul className="mt-1 space-y-1">
              {divs.slice(0, 8).map((d, i) => <li key={i} className="text-[11.5px] text-ink-2"><span className="num text-warn">{d.ts.slice(11, 16)}</span> {d.node} · {d.what}: observed {String(d.observed)} vs twin {String(d.twin)}</li>)}
            </ul>
          )}
        </div>
      </Glass>

      <div className="flex flex-col gap-3 min-h-0">
        <Glass className="p-4">
          <div className="flex items-center gap-2 text-[13px] font-semibold"><KeyRound size={14} className="text-warn" /> Device key rotation</div>
          <div className="text-[11px] text-ink-3 mt-1">New HMAC-SHA256 key for a device (shown once, stored pgcrypto-encrypted). The old key stays valid for 5 min, then its messages fail L2.</div>
          <div className="mt-2.5 flex gap-2">
            <input value={src} onChange={(e) => setSrc(e.target.value)} aria-label="Source id" className="flex-1 h-8 rounded-lg bg-black/30 border border-line-strong px-2 text-[12px] text-ink outline-none num" />
            <Button variant="solid" onClick={rotate}><RefreshCw size={13} /> Rotate</Button>
          </div>
          {rotated && <div className="num mt-2 text-[10px] text-ink-3 break-all">v{rotated.version} · {rotated.key.slice(0, 16)}… (copy to the device)</div>}
        </Glass>
        <Glass className="flex-1 flex flex-col min-h-0">
          <PanelHeader title="Audit log" sub={canSec ? "logins · applies · chaos · key rotations · Copilot" : "security role required"} right={<ScrollText size={14} className="text-ink-3" />} />
          <div className="flex-1 overflow-y-auto scroll-thin px-4 pb-3">
            {!canSec && <Button variant="ghost" onClick={() => ensureRole("security")}><ShieldAlert size={13} /> Sign in as security</Button>}
            {audit?.map((a, i) => (
              <div key={a.id ?? i} className="py-1.5 border-b border-line/50 text-[11.5px]">
                <span className="num text-ink-3">{(a.ts ?? "").slice(11, 19)}</span> <span className="text-ink">{a.user_id}</span> <span className="num text-ai">{a.action}</span>
                {a.target && <span className="text-ink-3"> · {a.target}</span>}
              </div>
            ))}
          </div>
        </Glass>
        <Glass className="p-3.5">
          <Eyebrow>Lowest reputation (L9)</Eyebrow>
          <div className="mt-1.5 space-y-1">
            {rep.slice(0, 4).map((r) => (
              <div key={r.source} className="flex items-center gap-2 text-[11.5px]"><span className="num text-ink-2 truncate flex-1">{r.source}</span>
                <span className="w-16"><Bar value={r.trust} max={1} color={r.trust < 0.4 ? C.bad : r.trust < 0.7 ? C.warn : C.ok} height={4} /></span>
                <span className="num w-9 text-right text-ink">{r.trust.toFixed(2)}</span></div>
            ))}
            {rep.length === 0 && <div className="text-[11.5px] text-ink-3">No telemetry yet.</div>}
          </div>
        </Glass>
      </div>
    </div>
  );
}
