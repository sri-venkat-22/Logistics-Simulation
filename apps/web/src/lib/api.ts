/**
 * REST client for the AEGIS API (services/api). Base URL from VITE_API_URL (lib/config.ts). The bearer token is the
 * signed-in user's JWT (lib/auth.ts; refreshed before expiry and once on a 401), or the dev token in development.
 */
import { bearer, useAuth } from "./auth";
import { API_URL, DEV_TOKEN, WS_URL } from "./config";

export { API_URL, WS_URL };

export class ApiError extends Error {
  constructor(public status: number, message: string) { super(message); }
}

export async function api<T>(path: string, init: RequestInit & { auth?: boolean } = {}, retried = false): Promise<T> {
  const headers: Record<string, string> = { "Content-Type": "application/json", ...(init.headers as Record<string, string>) };
  const tok = await bearer();
  if (tok && (init.auth || useAuth.getState().session)) headers.Authorization = `Bearer ${tok}`;
  const r = await fetch(`${API_URL}${path}`, { ...init, headers });
  if (r.status === 401 && !retried && useAuth.getState().session && (await useAuth.getState().refresh())) {
    return api<T>(path, init, true);
  }
  if (r.status === 401 && init.auth) useAuth.getState().setLoginOpen(true);
  if (!r.ok) {
    let msg = r.statusText;
    try { msg = (await r.json()).detail ?? msg; } catch { /* not JSON */ }
    throw new ApiError(r.status, typeof msg === "string" ? msg : JSON.stringify(msg));
  }
  return r.json() as Promise<T>;
}

// ------------------------------------------------------------------ contract types (mirrors services/api)
export interface ApiNode {
  id: string; type: "port" | "plant" | "supplier" | "dc" | "zone"; name: string; lat: number; lon: number;
  capacity: number | null; attrs: Record<string, unknown>; status: "up" | "degraded" | "closed"; twin_status: string;
  degree: number; tts_d: number | null; ttr_d: number | null; rei: number | null;
}
export interface ApiLane {
  id: string; from_id: string; to_id: string; mode: "road" | "rail" | "sea" | "air"; distance_km: number;
  cost_per_unit: number; capacity: number; co2_per_tkm: number; lt_mean_h: number; live_mult: number; extra_mult: number;
}
export interface ApiNetwork { nodes: ApiNode[]; lanes: ApiLane[]; skus: { id: string; family: string; name: string }[] }

export interface Band { p10: number; p50: number; p90: number; mean: number }
export interface Template {
  template: string; type: string; name: string | null; target: string; start: string; duration_h: number;
  severity: number; description: string | null; params: Record<string, unknown>; polygon: [number, number][] | null;
}
export interface ScenarioResult {
  n: number; seeds: number[]; spec_hash: string; days: number; wall_s: number;
  kpis: Record<string, Band>; per_sku_fill: Record<string, Band>;
  fill_rate_series: { t_days: number[]; p10: number[]; p50: number[]; p90: number[] };
  series: Record<string, { t_days: number[]; on_hand: { p10: number[]; p50: number[]; p90: number[] }; backlog: { p10: number[]; p50: number[]; p90: number[] } }>;
  stockout_prob: Record<string, number>;
  tts: { scenario: string; ttr_h: number | null; p_stockout: number; tts_h: Band | null; p_exposed: number }[];
}
export interface ScenarioStatus {
  id: string; kind: string; status: "queued" | "running" | "done" | "error"; cached: boolean; done: number; n: number;
  spec_hash: string; spec: Record<string, unknown>; baseline_id?: string; wall_s?: number; error?: string;
  result?: ScenarioResult; deltas?: Record<string, { scenario_p50: number; baseline_p50: number; delta_p50: number; delta_mean: number }>;
  plans?: PlanResult[];
  impact?: Impact;
}
export interface Impact {
  nodes: Record<string, number>;      // node -> remaining capacity factor (0 = closed)
  lanes: Record<string, number>;      // lane -> transit-time multiplier
  zones: Record<string, number>;      // zone -> demand multiplier
  dependents: string[];               // "DC/SKU" whose stock depends on what is disrupted
  polygons: [number, number][][];
}
export interface TwinEffect {
  id: string; label: string; kind: string; type: string; target: string; start_h: number; end_h: number | null;
  nodes: string[]; lanes: string[]; zones: string[];
}
export interface PlanAction {
  type: "set_route" | "transfer" | "policy" | "set_path"; dc?: string; sku?: string; lanes?: string[]; via?: string[];
  from?: string; to?: string; qty?: number; lead_h?: number; modes?: string[]; family?: string; dz?: number; path?: number;
}
export interface PlanResult {
  id: string; name: string; kind: string; actions: PlanAction[]; job: string; service: number; service_p10: number; otif: number;
  cost_lakh: number; co2_t: number; cvar95_lakh: number | null; shortfall_lakh: number | null; delay_h: number | null;
  backorders_p90: number; stockout_p: number; score: number; pareto: boolean; score_parts?: Record<string, number>;
  tts_p50_h: number | null; ttr_h: number | null; p_exposed: number | null;
  explanation?: { text: string; stockouts_avoided: string[]; evidence: Record<string, unknown>[] };
}
export interface Weights { service: number; risk: number; cost: number; co2: number }

export const getNetwork = () => api<ApiNetwork>("/api/v1/network");
export const getTemplates = () => api<Template[]>("/api/v1/scenarios/templates");
export const getScenario = (id: string, series = true) => api<ScenarioStatus>(`/api/v1/scenarios/${id}?series=${series}`);
export const createScenario = (body: { template: string; n: number; days: number }) =>
  api<ScenarioStatus>("/api/v1/scenarios", { method: "POST", body: JSON.stringify(body), auth: true });
export const optimizeScenario = (id: string, n = 100) =>
  api<{ candidates: { name: string; job: string }[] }>(`/api/v1/scenarios/${id}/optimize?n=${n}`, { method: "POST", auth: true });
export const applyPlan = (id: string) =>
  api<{ applied_by: string; acks: { ok: boolean; error?: string }[] }>(`/api/v1/plans/${id}/apply`, { method: "POST", auth: true });
export const rankPlans = (id: string, w: Weights) =>
  api<{ weights: Weights; plans: PlanResult[] }>(`/api/v1/scenarios/${id}/plans?${new URLSearchParams(Object.entries(w).map(([k, v]) => [k, String(v)]))}`);

export async function apiReachable(timeoutMs = 1500): Promise<boolean> {
  try {
    const ctl = new AbortController();
    const t = setTimeout(() => ctl.abort(), timeoutMs);
    const r = await fetch(`${API_URL}/healthz`, { signal: ctl.signal });
    clearTimeout(t);
    return r.ok;
  } catch {
    return false;
  }
}

export const pushDisruption = (template: string, start = "now") =>
  api<{ id: string; impact: Impact }>("/api/v1/disruptions", { method: "POST", body: JSON.stringify({ template, start }), auth: true });
export const listDisruptions = () => api<TwinEffect[]>("/api/v1/disruptions");
/** Shipments the fleet dispatched for applied plans: {shipment id: plan id}. */
export const plansDispatched = () => api<{ shipments: Record<string, string> }>("/api/v1/plans/dispatched");
export const endDisruption = (effectId: string) => api<{ ok: boolean }>(`/api/v1/disruptions/${effectId}`, { method: "DELETE", auth: true });

// ------------------------------------------------------------------ Phase 7: criticality, trust, ML, Copilot
export interface CriticalNode {
  rank: number; node: string; type: string; name: string; betweenness: number; flow_share: number; cascade_size: number;
  cascade_steps: number; unserved_share: number; rei: number | null; tts_days: number | null; ttr_days: number | null;
  exposed: boolean | null; spof_score: number;
}
export interface Cascade {
  node: string; alpha: number; unserved_share: number; size: number; failed_nodes: string[]; failed_lanes: string[];
  steps: { step: number; nodes: string[]; lanes: string[]; unserved_share: number }[];
}
export const getCriticality = (alpha = 0.25) => api<{ alpha: number; nodes: CriticalNode[] }>(`/api/v1/network/criticality?alpha=${alpha}`);
export const getCascade = (node: string, alpha = 0.25) => api<Cascade>(`/api/v1/network/cascade/${node}?alpha=${alpha}`);

export interface TrustLayer { id: string; name: string; technique: string; rejected: number; by_code: Record<string, number> }
export interface Divergence { node: string; what: string; observed: number | string; twin: number | string; ts: string }
export const getTrustLayers = () => api<{ layers: TrustLayer[]; divergences: Divergence[]; reputation: { source: string; trust: number; good: number; bad: number }[]; silent_sources: number }>("/api/v1/trust/layers");
export interface BenchType { attacks: number; detected: number; attack_recall: number; msgs: number; msgs_caught: number; msg_recall: number | null; ttd_p50_s: number | null; caught_by: Record<string, number> }
export interface Benchmark {
  messages: number; throughput_msgs_s: number; run_s: number;
  message_level: { tp: number; fp: number; fn: number; tn: number; precision: number; recall: number; f1: number; false_positive_rate: number };
  attack_level: { attacks: number; detected: number; recall: number }; by_type: Record<string, BenchType>;
}
export const getBenchmark = () => api<Benchmark>("/api/v1/trust/benchmark");
export interface AuditRow { id?: number; ts: string | null; user_id: string; action: string; target: string | null; details: Record<string, unknown> }
export const getAudit = (limit = 50) => api<AuditRow[]>(`/api/v1/audit?limit=${limit}`, { auth: true });
export const rotateDeviceKey = (sourceId: string, graceS = 300) =>
  api<{ source_id: string; version: number; key: string; previous_key_valid_until: string | null }>(
    `/api/v1/devices/${encodeURIComponent(sourceId)}/rotate?grace_s=${graceS}`, { method: "POST", auth: true });

export interface MlMetrics {
  eta: { twin: { dataset: { legs: number; test: number }; test: { mae_h: number; baseline_mae_h: number; p10_p90_coverage: number; n: number }; test_by_mode: Record<string, { mae_h: number; baseline_mae_h: number; p10_p90_coverage: number; n: number }> };
         dataco?: { dataset: { rows: number; test: number }; regression_days_real: { mae_days: number; baseline_mae_days: number; p10_p90_coverage: number }; late_delivery: { auc: number; baseline_auc_scheduled_days: number } } } | null;
  forecast: { wape: Record<string, number>; wape_festive_days: Record<string, number>; history: { test_window: string[]; series: number };
              policy_test: Record<string, { fill_rate: number; stockout_episodes: number; inventory_days: number; cost_lakh: number }> } | null;
  anomaly: { detectors: Record<string, { precision: number | null; recall: number | null; false_positive_rate: number; roc_auc: number }>; by_corruption: Record<string, Record<string, number>> } | null;
}
export const getMlMetrics = () => api<MlMetrics>("/api/v1/ml/metrics");
export const getCopilotStatus = () => api<{ mode: "claude" | "offline"; model: string | null }>("/api/v1/copilot/status");
export const getAuthConfig = () => api<{ env: string; demo_users: { user: string; role: string; password: string }[] }>("/api/v1/auth/config");

/** WebSocket URL with the session token attached (the API requires it in production). */
export function wsUrl(path: string): string {
  const tok = useAuth.getState().session?.access ?? DEV_TOKEN;
  return tok ? `${WS_URL}${path}${path.includes("?") ? "&" : "?"}token=${encodeURIComponent(tok)}` : `${WS_URL}${path}`;
}
