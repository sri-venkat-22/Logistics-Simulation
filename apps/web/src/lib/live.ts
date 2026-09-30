/**
 * Live state from WS /ws/live (msgpack snapshot + 5–10 Hz diffs) with client-side interpolation.
 *
 * Vehicles live in a mutable Map outside React state (thousands of rows at 5 Hz); React re-renders from the
 * animation clock. Each vehicle keeps its last two positions: on every update the previous *rendered* position
 * becomes `from`, the new fix becomes `to`, and `positionAt(now)` lerps between them over the vehicle's typical
 * update interval, so motion stays smooth at 60 fps on 5 Hz data and never jumps backwards. A short trail of past
 * fixes feeds the TripsLayer.
 */
import { decode } from "@msgpack/msgpack";
import { create } from "zustand";
import { WS_URL, getNetwork, type ApiNetwork } from "./api";
import type { Sev } from "./data";

export interface LiveVehicle {
  id: string; from: [number, number]; to: [number, number]; t0: number; t1: number; interval: number;
  speed: number; heading: number; scope: "national" | "city"; status: "live" | "predicted"; shipment: string | null;
  trail: [number, number, number][];   // [lon, lat, wall seconds]
}
export interface LiveAlert { id: string; sev: Sev; t: string; title: string; body: string; node: string | null; kind: string }
export interface LiveInv { on_hand: number; on_order: number; backlog: number; ts: string; s?: number }
export interface LiveKpis {
  ingest_rate: number; ingest_accepted: number; ingest_rejected: number; quarantined: number; vehicles_live: number;
  vehicles_predicted: number; city_vehicles: number; sources_silent: number; world_ts: string | null; ws_clients: number;
  twin?: { t_h: number; ts: string; fill_rate: number; units_demanded: number; backorder_units: number; shipments_active: number; shipments_delivered: number; effects: number };
}

const TRAIL = 14;
// Singletons on globalThis: a dev-server hot reload can load this module twice, and every copy must share one
// socket, one vehicle map and one store.
const G = globalThis as unknown as { __aegisVehicles?: Map<string, LiveVehicle>; __aegisLive?: unknown; __aegisWs?: { ws: WebSocket | null; retry: number; netTimer?: number } };
export const vehicles: Map<string, LiveVehicle> = (G.__aegisVehicles ??= new Map<string, LiveVehicle>());
const nowS = () => performance.now() / 1000;

export function positionAt(v: LiveVehicle, t: number): [number, number] {
  const f = v.t1 > v.t0 ? Math.min(1, Math.max(0, (t - v.t0) / (v.t1 - v.t0))) : 1;
  return [v.from[0] + (v.to[0] - v.from[0]) * f, v.from[1] + (v.to[1] - v.from[1]) * f];
}

type Row = [string, number, number, number, number, number, number, number, string | null];

function upsert(row: Row, t: number) {
  const [id, lon, lat, speed, heading, , scope, status, shipment] = row;
  const v = vehicles.get(id);
  if (!v) {
    vehicles.set(id, { id, from: [lon, lat], to: [lon, lat], t0: t, t1: t, interval: 1, speed, heading,
      scope: scope === 1 ? "city" : "national", status: status === 1 ? "predicted" : "live", shipment, trail: [[lon, lat, t]] });
    return;
  }
  const cur = positionAt(v, t);
  const gap = t - v.t0;
  v.interval = Math.min(5, Math.max(0.15, v.interval * 0.7 + gap * 0.3)); // EMA of the update interval
  const jump = Math.abs(cur[0] - lon) + Math.abs(cur[1] - lat) > 0.5;       // > ~50 km: don't glide across the map
  v.from = jump ? [lon, lat] : cur;
  v.to = [lon, lat];
  v.t0 = t;
  v.t1 = t + v.interval;
  v.speed = speed; v.heading = heading; v.status = status === 1 ? "predicted" : "live"; v.shipment = shipment;
  v.trail.push([lon, lat, t]);
  if (v.trail.length > TRAIL) v.trail.shift();
}

interface LiveStore {
  status: "idle" | "connecting" | "live" | "offline";
  network: ApiNetwork | null;
  inventory: Record<string, LiveInv>;
  ports: Record<string, { status: string; berth_queue: number; anchorage: number; berths_busy: number; berths_total: number }>;
  kpis: LiveKpis | null;
  kpiHistory: Record<string, number[]>;
  alerts: LiveAlert[];
  worldTs: string | null;
  frames: number;
  lastFrameMs: number;
  vehicleCount: number;
  dataVersion: number;
  connect: () => void;
  refreshNetwork: () => Promise<void>;
}

const conn = (G.__aegisWs ??= { ws: null, retry: 0 });

function pushHistory(h: Record<string, number[]>, k: LiveKpis): Record<string, number[]> {
  const add = (key: string, v: number) => ({ [key]: [...(h[key] ?? []), v].slice(-40) });
  return {
    ...h,
    ...add("fill", (k.twin?.fill_rate ?? 0) * 100),
    ...add("rate", k.ingest_rate),
    ...add("vehicles", k.vehicles_live),
    ...add("quarantined", k.quarantined),
  };
}

const makeStore = () => create<LiveStore>((set, get) => ({
  status: "idle",
  network: null,
  inventory: {},
  ports: {},
  kpis: null,
  kpiHistory: {},
  alerts: [],
  worldTs: null,
  frames: 0,
  lastFrameMs: 0,
  vehicleCount: 0,
  dataVersion: 0,

  refreshNetwork: async () => {
    try { set({ network: await getNetwork(), dataVersion: get().dataVersion + 1 }); } catch { /* keep the last copy */ }
  },

  connect: () => {
    if (conn.ws && (conn.ws.readyState === WebSocket.OPEN || conn.ws.readyState === WebSocket.CONNECTING)) return;
    set({ status: get().status === "live" ? "live" : "connecting" });
    const ws = new WebSocket(`${WS_URL}/ws/live`);
    conn.ws = ws;
    ws.binaryType = "arraybuffer";
    ws.onopen = () => {
      conn.retry = 0;
      set({ status: "live" });
      void get().refreshNetwork();
      window.clearInterval(conn.netTimer);
      conn.netTimer = window.setInterval(() => void get().refreshNetwork(), 5000);
    };
    ws.onmessage = (ev) => {
      const f = decode(new Uint8Array(ev.data as ArrayBuffer)) as Record<string, any>; // eslint-disable-line @typescript-eslint/no-explicit-any
      const t = nowS();
      const s = get();
      if (f.type === "snapshot") {
        vehicles.clear();
        for (const r of f.vehicles as Row[]) upsert(r, t);
        set({ inventory: f.inventory, ports: f.ports, kpis: f.kpis, kpiHistory: pushHistory(s.kpiHistory, f.kpis),
          alerts: f.alerts, worldTs: f.world_ts, frames: s.frames + 1, lastFrameMs: Date.now(), vehicleCount: vehicles.size,
          dataVersion: s.dataVersion + 1 });
        return;
      }
      for (const r of f.vehicles.u as Row[]) upsert(r, t);
      for (const id of f.vehicles.r as string[]) vehicles.delete(id);
      const patch: Partial<LiveStore> = { frames: s.frames + 1, lastFrameMs: Date.now(), vehicleCount: vehicles.size, worldTs: f.world_ts ?? s.worldTs };
      if (f.inventory && Object.keys(f.inventory).length) { patch.inventory = { ...s.inventory, ...f.inventory }; patch.dataVersion = s.dataVersion + 1; }
      if (f.ports && Object.keys(f.ports).length) { patch.ports = { ...s.ports, ...f.ports }; patch.dataVersion = s.dataVersion + 1; }
      if (f.alerts?.length) patch.alerts = [...(f.alerts as LiveAlert[]).reverse(), ...s.alerts].slice(0, 120);
      if (f.kpis) { patch.kpis = f.kpis; patch.kpiHistory = pushHistory(s.kpiHistory, f.kpis); }
      set(patch);
    };
    ws.onclose = () => {
      conn.ws = null;
      window.clearInterval(conn.netTimer);
      set({ status: "offline" });
      conn.retry = Math.min(conn.retry + 1, 6);
      window.setTimeout(() => get().connect(), 500 * 2 ** conn.retry); // reconnect with backoff; the snapshot re-syncs
    };
    ws.onerror = () => ws.close();
  },
}));

export const useLive: ReturnType<typeof makeStore> = (G.__aegisLive ??= makeStore()) as ReturnType<typeof makeStore>;

// Connect as soon as the module loads (idempotent: a second module copy finds the socket already open).
if (typeof window !== "undefined") useLive.getState().connect();
