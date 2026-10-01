/**
 * Fidelity Lab on the live API: measured model quality (Phase 7.3) and the trust benchmark (7.5). Every number is
 * read from the reports the training / evaluation scripts write (docs/ml/*.json, docs/trust/benchmark.json).
 */
import { useEffect, useState } from "react";
import clsx from "clsx";
import { Bar, Eyebrow, Glass, PanelHeader } from "../components/ui";
import { getBenchmark, getMlMetrics, type Benchmark, type MlMetrics } from "../lib/api";
import { C } from "../lib/theme";

const pct = (v: number, d = 1) => `${(v * 100).toFixed(d)} %`;

function Stat({ label, value, sub, good }: { label: string; value: string; sub?: string; good?: boolean }) {
  return (
    <div className="rounded-lg bg-black/25 px-3 py-2">
      <div className="eyebrow !text-[9px]">{label}</div>
      <div className={clsx("num text-[18px]", good === undefined ? "text-ink" : good ? "text-good" : "text-warn")}>{value}</div>
      {sub && <div className="num text-[10px] text-ink-3">{sub}</div>}
    </div>
  );
}

export default function FidelityLabLive() {
  const [m, setM] = useState<MlMetrics | null>(null);
  const [b, setB] = useState<Benchmark | null>(null);
  useEffect(() => { getMlMetrics().then(setM).catch(() => undefined); getBenchmark().then(setB).catch(() => undefined); }, []);
  const eta = m?.eta;
  const fc = m?.forecast;
  const an = m?.anomaly;
  const methods: [string, string][] = [["naive", "Seasonal naive"], ["ets", "AutoETS"], ["ets_cal", "AutoETS + calendar"], ["model", "Planning model"]];
  const worst = fc ? Math.max(...Object.values(fc.wape)) : 1;

  return (
    <div className="absolute inset-0 pt-14 px-4 pb-4 grid grid-cols-2 grid-rows-2 gap-3 min-h-0">
      <Glass className="flex flex-col min-h-0">
        <PanelHeader title="ETA models · LightGBM quantiles" sub="P10 / P50 / P90 transit time, held-out last 20 % (time split)" />
        {eta ? (
          <div className="px-4 pb-3 flex flex-col gap-3 overflow-y-auto scroll-thin">
            <div className="grid grid-cols-3 gap-2">
              <Stat label="Twin MAE" value={`${eta.twin.test.mae_h.toFixed(2)} h`} sub={`schedule ${eta.twin.test.baseline_mae_h.toFixed(2)} h`} good={eta.twin.test.mae_h < eta.twin.test.baseline_mae_h} />
              <Stat label="P10–P90 coverage" value={pct(eta.twin.test.p10_p90_coverage)} sub="target ≈ 80 %" good={Math.abs(eta.twin.test.p10_p90_coverage - 0.8) < 0.05} />
              <Stat label="Legs" value={eta.twin.dataset.legs.toLocaleString("en-IN")} sub={`test ${eta.twin.test.n.toLocaleString("en-IN")}`} />
            </div>
            <table className="w-full text-[11.5px]"><tbody>
              {Object.entries(eta.twin.test_by_mode).map(([mode, r]) => (
                <tr key={mode} className="border-t border-line/60"><td className="py-1 text-ink-2 capitalize">{mode}</td>
                  <td className="num text-right text-ink">{r.mae_h.toFixed(2)} h</td><td className="num text-right text-ink-3">vs {r.baseline_mae_h.toFixed(2)} h</td>
                  <td className="num text-right text-ink-3">{pct(r.p10_p90_coverage, 0)} in band</td></tr>
              ))}
            </tbody></table>
            {eta.dataco && (
              <div>
                <Eyebrow>DataCo real shipments ({eta.dataco.dataset.rows.toLocaleString("en-IN")} orders)</Eyebrow>
                <div className="grid grid-cols-3 gap-2 mt-1.5">
                  <Stat label="Days MAE" value={eta.dataco.regression_days_real.mae_days.toFixed(3)} sub={`scheduled ${eta.dataco.regression_days_real.baseline_mae_days.toFixed(3)}`} good />
                  <Stat label="Late-delivery AUC" value={eta.dataco.late_delivery.auc.toFixed(3)} sub={`baseline ${eta.dataco.late_delivery.baseline_auc_scheduled_days.toFixed(3)}`} good />
                  <Stat label="Coverage" value={pct(eta.dataco.regression_days_real.p10_p90_coverage)} />
                </div>
              </div>
            )}
          </div>
        ) : <div className="px-4 text-[12px] text-ink-3">python -m ml.eta</div>}
      </Glass>

      <Glass className="flex flex-col min-h-0">
        <PanelHeader title="Demand forecast → (s,S)" sub={fc ? `${fc.history.series} zone × SKU series · test ${fc.history.test_window[0]} → ${fc.history.test_window[1]} (Diwali ramp)` : "python -m ml.forecast"} />
        {fc && (
          <div className="px-4 pb-3 flex flex-col gap-2.5 overflow-y-auto scroll-thin">
            {methods.map(([k, label]) => (
              <div key={k} className="grid grid-cols-[130px_1fr_60px] items-center gap-2 text-[11.5px]">
                <span className="text-ink-2">{label}</span><Bar value={fc.wape[k]} max={worst} color={k === "ets_cal" ? C.ok : C.warn} />
                <span className="num text-right text-ink">{pct(fc.wape[k])}</span>
              </div>
            ))}
            <div className="text-[10.5px] text-ink-3">WAPE = Σ|error| / Σ demand. Festive days: AutoETS {pct(fc.wape_festive_days.ets)}, + calendar {pct(fc.wape_festive_days.ets_cal)}.</div>
            <Eyebrow className="mt-1">Same reality, two reorder-point sources</Eyebrow>
            <div className="grid grid-cols-2 gap-2">
              {Object.entries(fc.policy_test).map(([k, v]) => (
                <div key={k} className={clsx("rounded-lg px-3 py-2 border", k === "ets_calendar" ? "border-ok/40 bg-ok/6" : "border-line")}>
                  <div className="text-[11.5px] text-ink">{k === "ets_calendar" ? "AutoETS + calendar" : "Planning model"}</div>
                  <div className="num text-[11px] text-ink-2">fill {pct(v.fill_rate)} · {v.stockout_episodes} stock-outs · ₹{v.cost_lakh.toFixed(0)} L</div>
                </div>
              ))}
            </div>
          </div>
        )}
      </Glass>

      <Glass className="flex flex-col min-h-0">
        <PanelHeader title="Feed anomaly detection (L8)" sub="Robust MAD z-score + IsolationForest on ASNs, clean vs corrupted copies" />
        {an && (
          <div className="px-4 pb-3 overflow-y-auto scroll-thin">
            <table className="w-full text-[11.5px]">
              <thead><tr className="text-ink-3 text-left"><th className="font-normal py-1">Detector</th><th className="font-normal text-right">precision</th><th className="font-normal text-right">recall</th><th className="font-normal text-right">FPR</th><th className="font-normal text-right">AUC</th></tr></thead>
              <tbody>{Object.entries(an.detectors).map(([k, v]) => (
                <tr key={k} className="border-t border-line/60"><td className="py-1 text-ink-2">{k === "l8_rule" ? "L8 rule (deployed)" : k}</td>
                  <td className="num text-right">{v.precision?.toFixed(3) ?? "—"}</td><td className="num text-right">{v.recall?.toFixed(3) ?? "—"}</td>
                  <td className="num text-right">{pct(v.false_positive_rate, 2)}</td><td className="num text-right">{v.roc_auc.toFixed(3)}</td></tr>
              ))}</tbody>
            </table>
            <div className="num mt-2 text-[10.5px] text-ink-3">L8 recall by corruption: {Object.entries(an.by_corruption).map(([k, v]) => `${k} ${pct(v.l8_rule, 0)}`).join(" · ")}</div>
            <div className="text-[10.5px] text-ink-3 mt-1">An under-reported ASN (×0.2) looks like a partial truck-load; stock reconciliation (RECON_MISMATCH) catches phantom stock instead.</div>
          </div>
        )}
      </Glass>

      <Glass className="flex flex-col min-h-0">
        <PanelHeader title="Attack detection · 9-layer pipeline" sub={b ? `${b.attack_level.attacks} labelled attacks in ${b.messages.toLocaleString("en-IN")} messages` : "python -m services.api.app.trust_bench"} />
        {b && (
          <div className="px-4 pb-3 flex flex-col gap-2.5 overflow-y-auto scroll-thin">
            <div className="grid grid-cols-4 gap-2">
              <Stat label="Attacks caught" value={pct(b.attack_level.recall)} good />
              <Stat label="Msg precision" value={b.message_level.precision.toFixed(3)} good />
              <Stat label="Msg recall" value={b.message_level.recall.toFixed(3)} />
              <Stat label="False pos." value={pct(b.message_level.false_positive_rate, 2)} good />
            </div>
            {Object.entries(b.by_type).map(([t, r]) => (
              <div key={t} className="grid grid-cols-[120px_1fr_48px] items-center gap-2 text-[11px]">
                <span className="text-ink-2">{t.replace("_", " ")}</span><Bar value={r.attack_recall} max={1} color={r.attack_recall === 1 ? C.ok : r.attack_recall > 0.8 ? C.warn : C.bad} height={5} />
                <span className="num text-right text-ink">{r.detected}/{r.attacks}</span>
              </div>
            ))}
          </div>
        )}
      </Glass>
    </div>
  );
}
