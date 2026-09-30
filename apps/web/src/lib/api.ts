/**
 * REST client for the AEGIS API (services/api). Base URL from VITE_API_URL (default http://localhost:8000);
 * the bearer token from VITE_API_TOKEN (default the local-dev planner token — Phase 8 replaces it with login).
 */
export const API_URL: string = (import.meta.env.VITE_API_URL as string | undefined) ?? "http://localhost:8000";
export const WS_URL = API_URL.replace(/^http/, "ws");
const TOKEN: string = (import.meta.env.VITE_API_TOKEN as string | undefined) ?? "dev-planner";

export class ApiError extends Error {
  constructor(public status: number, message: string) { super(message); }
}

export async function api<T>(path: string, init: RequestInit & { auth?: boolean } = {}): Promise<T> {
  const headers: Record<string, string> = { "Content-Type": "application/json", ...(init.headers as Record<string, string>) };
  if (init.auth) headers.Authorization = `Bearer ${TOKEN}`;
  const r = await fetch(`${API_URL}${path}`, { ...init, headers });
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
  cost_per_unit: number; capacity: number; co2_per_tkm: number; lt_mean_h: number; live_mult: number;
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
}
export interface PlanResult {
  id: string; name: string; actions: Record<string, unknown>[]; job: string; service: number; service_p10: number; otif: number;
  cost_lakh: number; co2_t: number; backorders_p90: number; stockout_p: number; score: number;
}

export const getNetwork = () => api<ApiNetwork>("/api/v1/network");
export const getTemplates = () => api<Template[]>("/api/v1/scenarios/templates");
export const getScenario = (id: string, series = true) => api<ScenarioStatus>(`/api/v1/scenarios/${id}?series=${series}`);
export const createScenario = (body: { template: string; n: number; days: number }) =>
  api<ScenarioStatus>("/api/v1/scenarios", { method: "POST", body: JSON.stringify(body), auth: true });
export const optimizeScenario = (id: string, n = 100) =>
  api<{ candidates: { name: string; job: string }[] }>(`/api/v1/scenarios/${id}/optimize?n=${n}`, { method: "POST", auth: true });
export const applyPlan = (id: string) => api<{ applied_by: string }>(`/api/v1/plans/${id}/apply`, { method: "POST", auth: true });

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
