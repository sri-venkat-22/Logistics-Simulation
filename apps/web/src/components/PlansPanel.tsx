/**
 * Ranked plans (Phase 7.1): every candidate evaluated with Monte Carlo on the scenario's seeds. Weight sliders
 * re-rank server-side (GET /scenarios/{id}/plans); Pareto-optimal plans are marked; each plan explains itself from
 * an evidence replication; Apply needs the planner role and is audited.
 */
import { useEffect, useRef, useState } from "react";
import { toast } from "sonner";
import clsx from "clsx";
import { Check, ChevronDown, ChevronRight, GitBranch } from "lucide-react";
import { applyPlan, rankPlans, type PlanResult, type Weights } from "../lib/api";
import { ensureRole } from "../lib/auth";
import { Badge, Button, Eyebrow } from "./ui";

const DEFAULT: Weights = { service: 0.4, risk: 0.3, cost: 0.2, co2: 0.1 };
const LABEL: Record<keyof Weights, string> = { service: "Service", risk: "Risk", cost: "Cost", co2: "CO₂" };
const HINT: Record<keyof Weights, string> = { service: "P50 fill rate", risk: "CVaR95 of the shortfall value (mean of the worst 5 % of runs)",
  cost: "P50 total cost", co2: "P50 CO₂" };
const KIND: Record<string, string> = { baseline: "baseline", reroute: "reroute", resource: "sourcing", reallocate: "transfer",
  expedite: "expedite", buffer: "buffer", combined: "combined" };

export function PlansPanel({ scenarioId, initial }: { scenarioId: string; initial: PlanResult[] }) {
  const [plans, setPlans] = useState(initial);
  const [w, setW] = useState<Weights>(DEFAULT);
  const [open, setOpen] = useState<string | null>(null);
  const [applied, setApplied] = useState<string | null>(null);
  const timer = useRef<number | undefined>(undefined);
  useEffect(() => setPlans(initial), [initial]);
  useEffect(() => {
    if (Object.entries(w).every(([k, v]) => DEFAULT[k as keyof Weights] === v)) { setPlans(initial); return; }
    window.clearTimeout(timer.current);
    timer.current = window.setTimeout(() => { rankPlans(scenarioId, w).then((r) => setPlans(r.plans)).catch(() => undefined); }, 250);
    return () => window.clearTimeout(timer.current);
  }, [w, scenarioId, initial]);
  const base = plans.find((p) => p.kind === "baseline");

  const apply = (p: PlanResult) => {
    if (!ensureRole("planner")) return;
    applyPlan(p.id).then((r) => {
      setApplied(p.id);
      const ok = r.acks.filter((a) => a.ok).length;
      toast.success(`Applied: ${p.name}`, { description: `${ok}/${r.acks.length} actions pushed to the live twin by ${r.applied_by}` });
    }).catch((e) => toast.error("Apply failed", { description: String(e) }));
  };

  return (
    <div className="mt-3">
      <div className="flex items-center gap-2"><Eyebrow>Ranked plans · same seeds (CRN)</Eyebrow><span className="text-[10.5px] text-ink-3 ml-auto">drag to re-weight</span></div>
      <div className="grid grid-cols-2 gap-x-3 gap-y-1 mt-1.5 mb-2">
        {(Object.keys(DEFAULT) as (keyof Weights)[]).map((k) => (
          <label key={k} className="flex items-center gap-1.5 text-[10.5px] text-ink-3 min-w-0" title={HINT[k]}>
            <span className="w-[42px] shrink-0">{LABEL[k]}</span>
            <input type="range" min={0} max={1} step={0.05} value={w[k]} aria-label={`${LABEL[k]} weight`}
              onChange={(e) => setW({ ...w, [k]: Number(e.target.value) })} className="flex-1 min-w-0 accent-[var(--color-ai,#A78BFA)]" />
            <span className="num w-7 text-right text-ink-2">{w[k].toFixed(2)}</span>
          </label>
        ))}
      </div>
      <div className="space-y-1.5">
        {plans.map((p, i) => {
          const d = (x: number | null | undefined, y: number | null | undefined) => (x ?? 0) - (y ?? 0);
          return (
            <div key={p.id} className={clsx("rounded-xl border", i === 0 ? "border-ai/50 bg-ai/8" : "border-line")}>
              <div className="px-3 py-2 flex items-center gap-2.5">
                <button onClick={() => setOpen(open === p.id ? null : p.id)} className="text-ink-3 hover:text-ink cursor-pointer" aria-label="Details">
                  {open === p.id ? <ChevronDown size={14} /> : <ChevronRight size={14} />}
                </button>
                <div className="min-w-0 flex-1">
                  <div className="flex items-center gap-1.5">
                    <span className="text-[12.5px] text-ink truncate">{p.name}</span>
                    {p.pareto && <Badge sev="good" className="!text-[9px]"><GitBranch size={9} /> Pareto</Badge>}
                    <span className="text-[9.5px] uppercase tracking-wider text-ink-3">{KIND[p.kind] ?? p.kind}</span>
                  </div>
                  <div className="num text-[10.5px] text-ink-3">
                    fill {(p.service * 100).toFixed(2)}% · ₹{p.cost_lakh.toFixed(1)} L · CO₂ {p.co2_t.toFixed(1)} t · CVaR95 ₹{(p.cvar95_lakh ?? 0).toFixed(1)} L · score {p.score.toFixed(2)}
                  </div>
                </div>
                {p.kind !== "baseline" && (
                  <Button variant={i === 0 ? "primary" : "ghost"} disabled={applied === p.id} onClick={() => apply(p)}>
                    {applied === p.id ? <><Check size={13} /> Applied</> : "Apply"}
                  </Button>
                )}
              </div>
              {open === p.id && (
                <div className="px-3 pb-2.5 text-[11.5px] text-ink-2 space-y-1.5 border-t border-line/60 pt-2">
                  {p.explanation?.text && <p className="leading-relaxed">{p.explanation.text}</p>}
                  {base && p !== base && (
                    <div className="num text-[10.5px] text-ink-3">vs do nothing: fill {d(p.service, base.service) >= 0 ? "+" : ""}{(d(p.service, base.service) * 100).toFixed(2)} pp ·
                      cost {d(p.cost_lakh, base.cost_lakh) >= 0 ? "+" : ""}{d(p.cost_lakh, base.cost_lakh).toFixed(1)} L · CVaR95 {d(p.cvar95_lakh, base.cvar95_lakh) >= 0 ? "+" : ""}{d(p.cvar95_lakh, base.cvar95_lakh).toFixed(1)} L</div>
                  )}
                  <ul className="space-y-0.5">
                    {p.actions.slice(0, 8).map((a, j) => (
                      <li key={j} className="num text-[10.5px] text-ink-3">
                        {a.type === "set_route" && `route ${a.dc?.replace("DC_", "")} ${a.sku?.replace("SKU_", "")} via ${(a.via ?? []).join(" → ") || "direct"} (${a.lead_h} h)`}
                        {a.type === "transfer" && `transfer ${a.qty?.toLocaleString("en-IN")} ${a.sku?.replace("SKU_", "")} ${a.from?.replace("DC_", "")} → ${a.to?.replace("DC_", "")} by ${(a.modes ?? []).join("+")} (${a.lead_h} h)`}
                        {a.type === "policy" && `safety factor +${a.dz} for ${a.family}`}
                        {a.type === "set_path" && `sourcing path ${a.path} for ${a.dc} ${a.sku}`}
                      </li>
                    ))}
                    {p.actions.length > 8 && <li className="text-[10.5px] text-ink-3">… {p.actions.length - 8} more</li>}
                  </ul>
                </div>
              )}
            </div>
          );
        })}
      </div>
    </div>
  );
}
