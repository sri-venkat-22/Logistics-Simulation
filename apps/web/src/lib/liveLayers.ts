/**
 * deck.gl layers for the Control Tower on live data (Phase 5):
 *   nodes      ScatterplotLayer (+ pulsing ring on nodes that are disrupted or at risk)
 *   lanes      ArcLayer, coloured by status (a live lane-time multiplier > 1 = at risk)
 *   trucks     TripsLayer trails of recent fixes + ScatterplotLayer heads at the interpolated position
 *   inventory  ColumnLayer 3-D bars of observed on-hand stock per DC
 */
import { ArcLayer, ColumnLayer, ScatterplotLayer, TextLayer } from "@deck.gl/layers";
import { TripsLayer } from "@deck.gl/geo-layers";
import type { Layer } from "@deck.gl/core";
import type { ApiLane, ApiNetwork, ApiNode, Impact } from "./api";
import { positionAt, type LiveInv, type LiveVehicle } from "./live";
import { RGB } from "./theme";

type RGBA = [number, number, number, number];
const withA = (c: readonly [number, number, number], a: number): RGBA => [c[0], c[1], c[2], a];

export type LiveStatus = "ok" | "warn" | "bad" | "zone";

export function nodeLiveStatus(n: ApiNode, inventory: Record<string, LiveInv>, ports: Record<string, { status: string }>): LiveStatus {
  if (n.type === "zone") return "zone";
  const port = ports[n.id]?.status ?? n.status;
  if (port === "closed" || n.twin_status === "closed") return "bad";          // observed, or disrupted in the live twin
  if (port === "degraded" || n.twin_status === "degraded") return "warn";
  let st: LiveStatus = "ok";
  for (const [k, v] of Object.entries(inventory)) {
    if (!k.startsWith(`${n.id}/`)) continue;
    if (v.backlog > 0) return "bad";
    if (v.s !== undefined && v.on_hand < v.s) st = "warn";
  }
  return st;
}

const statusRGB = { ok: RGB.ok, warn: RGB.warn, bad: RGB.bad, zone: RGB.dim } as const;

export interface LiveLayerState {
  t: number;                          // wall seconds (performance.now / 1000)
  pulse: number;                      // animation clock (s)
  net: ApiNetwork;
  vehicles: LiveVehicle[];
  inventory: Record<string, LiveInv>;
  ports: Record<string, { status: string }>;
  layers: Record<string, boolean>;
  selectedNode: string | null;
  version: number;                    // bumps when inventory / ports / network change (updateTriggers)
  zoom?: number;                      // current map zoom: national-scale bars are hidden at city zoom
  impact?: Impact | null;             // Scenario Lab: what the what-if disrupts (drawn in red)
  risk?: Record<string, number>;      // Scenario Lab: node -> added stock-out probability (amber -> red)
  trailSeconds?: number;
}

export function liveLayers(s: LiveLayerState): Layer[] {
  const L = s.layers;
  const byId = new Map(s.net.nodes.map((n) => [n.id, n]));
  const status = new Map(s.net.nodes.map((n) => [n.id, nodeLiveStatus(n, s.inventory, s.ports)]));
  if (s.impact) {  // a what-if colours what it disrupts and the DCs it puts at risk
    for (const [id, f] of Object.entries(s.impact.nodes)) status.set(id, f < 0.25 ? "bad" : "warn");
    for (const [id, p] of Object.entries(s.risk ?? {})) if (p > 0.005 && status.get(id) !== "bad") status.set(id, p >= 0.2 ? "bad" : "warn");
  }
  const out: Layer[] = [];

  if (L.lanes) {
    out.push(new ArcLayer<ApiLane>({
      id: "live-lanes",
      data: s.net.lanes,
      getSourcePosition: (d) => { const a = byId.get(d.from_id)!; return [a.lon, a.lat]; },
      getTargetPosition: (d) => { const b = byId.get(d.to_id)!; return [b.lon, b.lat]; },
      getSourceColor: (d) => laneColor(d, status, 0.55, s.impact),
      getTargetColor: (d) => laneColor(d, status, 0.9, s.impact),
      getWidth: (d) => (d.live_mult > 1.02 || d.extra_mult > 1.02 || (s.impact && d.id in s.impact.lanes) || status.get(d.to_id) === "bad"
        || status.get(d.from_id) === "bad" ? 2.6 : 1.1),
      getHeight: (d) => ({ road: 0.15, rail: 0.25, sea: 0.5, air: 0.9 })[d.mode],
      greatCircle: true,
      widthUnits: "pixels",
      pickable: true,
      updateTriggers: { getSourceColor: [s.version, s.impact], getTargetColor: [s.version, s.impact], getWidth: [s.version, s.impact] },
      parameters: { depthCompare: "always" },
    }));
  }

  if (L.trucks) {
    const trail = s.trailSeconds ?? 25;
    const trips = s.vehicles.filter((v) => v.trail.length > 1);
    out.push(new TripsLayer<LiveVehicle>({
      id: "live-trails",
      data: trips,
      getPath: (d) => [...d.trail.map((p) => [p[0], p[1]] as [number, number]), positionAt(d, s.t)],
      getTimestamps: (d) => [...d.trail.map((p) => p[2]), s.t],
      getColor: (d) => (d.status === "predicted" ? RGB.warn : d.scope === "city" ? RGB.good : RGB.ok),
      currentTime: s.t,
      trailLength: trail,
      widthMinPixels: 2.2,
      capRounded: true, jointRounded: true,
      opacity: 0.85,
      updateTriggers: { getPath: s.t, getTimestamps: s.t },
      parameters: { depthCompare: "always" },
    }));
    out.push(new ScatterplotLayer<LiveVehicle>({
      id: "live-trucks",
      data: s.vehicles,
      getPosition: (d) => positionAt(d, s.t),
      getFillColor: (d) => (d.status === "predicted" ? withA(RGB.warn, 230) : withA(RGB.ink, 235)),
      getRadius: (d) => (d.scope === "city" ? 3.2 : 2.6),
      radiusUnits: "pixels",
      stroked: true,
      getLineColor: (d) => (d.status === "predicted" ? withA(RGB.warn, 120) : [7, 11, 20, 220]),
      lineWidthMinPixels: (1),
      pickable: true,
      updateTriggers: { getPosition: s.t, getFillColor: s.t, getLineColor: s.t },
      parameters: { depthCompare: "always" },
    }));
  }

  if (L.inventory && (s.zoom ?? 0) < 7.5) {
    const dcs = s.net.nodes.filter((n) => n.type === "dc");
    out.push(new ColumnLayer<ApiNode>({
      id: "live-inventory",
      data: dcs,
      diskResolution: 12,
      radius: 11000,
      extruded: true,
      getPosition: (d) => [d.lon + 0.18, d.lat - 0.12],
      getElevation: (d) => {
        let total = 0;
        for (const [k, v] of Object.entries(s.inventory)) if (k.startsWith(`${d.id}/`)) total += v.on_hand;
        return 20_000 + Math.sqrt(Math.max(0, total)) * 1_600;  // sqrt keeps FMCG hubs from dwarfing vaccine DCs
      },
      getFillColor: (d) => withA(statusRGB[status.get(d.id) ?? "ok"], 210),
      material: { ambient: 0.5, diffuse: 0.6, shininess: 40, specularColor: [80, 80, 80] },
      pickable: true,
      updateTriggers: { getElevation: s.version, getFillColor: s.version },
      transitions: { getElevation: 600 },
    }));
  }

  if (L.nodes) {
    const phase = (s.pulse % 1.6) / 1.6;
    const alarm = s.net.nodes.filter((n) => ["bad", "warn"].includes(status.get(n.id)!));
    out.push(new ScatterplotLayer<ApiNode>({
      id: "live-pulse",
      data: alarm,
      getPosition: (d) => [d.lon, d.lat],
      getRadius: 10 + phase * 26,
      radiusUnits: "pixels",
      stroked: true, filled: false,
      getLineColor: (d) => withA(statusRGB[status.get(d.id)!], 255 * (1 - phase)),
      lineWidthMinPixels: 2,
      updateTriggers: { getRadius: s.pulse, getLineColor: [s.pulse, s.version, s.impact, s.risk] },
      parameters: { depthCompare: "always" },
    }));
    out.push(new ScatterplotLayer<ApiNode>({
      id: "live-nodes",
      data: s.net.nodes,
      getPosition: (d) => [d.lon, d.lat],
      getRadius: (d) => ({ port: 7, plant: 6, supplier: 5.5, dc: 7, zone: 3.5 })[d.type] * (s.selectedNode === d.id ? 1.5 : 1),
      radiusUnits: "pixels",
      getFillColor: (d) => (d.type === "zone" ? [154, 167, 189, 150] : withA(statusRGB[status.get(d.id)!], 255)),
      stroked: true,
      getLineColor: (d) => (s.selectedNode === d.id ? [255, 255, 255, 255] : [7, 11, 20, 255]),
      lineWidthMinPixels: 2,
      pickable: true,
      updateTriggers: { getFillColor: [s.version, s.impact, s.risk], getRadius: s.selectedNode, getLineColor: s.selectedNode },
      parameters: { depthCompare: "always" },
    }));
    out.push(new TextLayer<ApiNode>({
      id: "live-labels",
      data: s.net.nodes.filter((n) => n.type === "dc" || n.type === "port"),
      getPosition: (d) => [d.lon, d.lat],
      getText: (d) => d.name.split(" (")[0],
      getSize: 11,
      getColor: [230, 237, 247, 200],
      getPixelOffset: [12, 0],
      getTextAnchor: "start",
      getAlignmentBaseline: "center",
      fontFamily: "Geist Variable, Inter, sans-serif",
      fontWeight: 600,
      outlineWidth: 3,
      outlineColor: [7, 11, 20, 240],
      fontSettings: { sdf: true },
      characterSet: "auto",
      parameters: { depthCompare: "always" },
    }));
  }
  return out;
}

function laneColor(l: ApiLane, status: Map<string, LiveStatus>, a: number, impact?: Impact | null): RGBA {
  if ((impact && l.id in impact.lanes) || l.extra_mult > 1.02) return withA(RGB.bad, 230 * a);
  if (status.get(l.to_id) === "bad" || status.get(l.from_id) === "bad") return withA(RGB.bad, 200 * a);
  if (l.live_mult > 1.02) return withA(RGB.warn, 220 * a);
  return withA(RGB.ok, 150 * a);
}
