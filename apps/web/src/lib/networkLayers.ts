import { ScatterplotLayer, PathLayer, ColumnLayer, TextLayer, PolygonLayer } from "@deck.gl/layers";
import { TripsLayer } from "@deck.gl/geo-layers";
import { HeatmapLayer } from "@deck.gl/aggregation-layers";
import type { Layer } from "@deck.gl/core";
import { control, network, scenario, shipments, laneById, nodeById, type Lane, type NetNode } from "./data";
import { arcPath, normalisedTimestamps, pointAt, rng, type LonLatAlt } from "./geo";
import { RGB } from "./theme";

const HEIGHT: Record<Lane["mode"], number> = { road: 0.03, rail: 0.05, sea: 0.1, air: 0.22 };
const LOOP = 60; // seconds per particle loop

interface LaneGeo { lane: Lane; path: LonLatAlt[]; ts: number[] }
export const laneGeo: LaneGeo[] = network.lanes.map((lane) => {
  const a = nodeById.get(lane.from_id)!, b = nodeById.get(lane.to_id)!;
  const path = arcPath([a.lon, a.lat], [b.lon, b.lat], lane.mode === "sea" ? 72 : 40, HEIGHT[lane.mode]);
  return { lane, path, ts: normalisedTimestamps(path) };
});
const geoByLane = new Map(laneGeo.map((g) => [g.lane.id, g]));

interface Particle { laneId: string; path: LonLatAlt[]; timestamps: number[] }
const particles: Particle[] = (() => {
  const r = rng(7);
  const out: Particle[] = [];
  for (const g of laneGeo) {
    const n = Math.max(2, Math.round(g.lane.capacity / 2500));
    const dur = { road: 14, rail: 20, sea: 34, air: 7 }[g.lane.mode];
    for (let i = 0; i < n; i++) {
      const off = r() * LOOP;
      for (const shift of [0, -LOOP, LOOP]) {
        out.push({ laneId: g.lane.id, path: g.path, timestamps: g.ts.map((t) => off + shift + t * dur) });
      }
    }
  }
  return out;
})();

export interface NetState {
  t: number;
  timeOffsetH?: number;
  scenarioActive?: boolean;   // Chennai disruption is on the map
  mitigated?: boolean;        // plan A applied (or mitigated view in split screen)
  layers?: Record<string, boolean>;
  selectedNode?: string | null;
  drawProgress?: number;      // 0..1, intro "arcs draw in one by one"
  showLabels?: boolean;
  idPrefix?: string;
}

type RGBA = [number, number, number, number];

/** Hand-placed labels for the nodes a viewer must find in 3 seconds (the national view is dense around Hyderabad). */
const LABELS: { id: string; text: string; anchor: "start" | "end"; offset: [number, number] }[] = [
  { id: "DC_HYD_MEDCHAL", text: "HYD-Medchal DC", anchor: "start", offset: [12, -9] },
  { id: "DC_HYD_SHAMSHABAD", text: "HYD-Shamshabad DC", anchor: "start", offset: [12, 9] },
  { id: "PLANT_PATANCHERU", text: "Patancheru plant", anchor: "end", offset: [-12, -2] },
  { id: "PORT_CHENNAI", text: "Chennai Port", anchor: "start", offset: [12, 0] },
  { id: "PORT_VIZAG", text: "Vizag Port", anchor: "start", offset: [12, 0] },
  { id: "PORT_JNPT", text: "JNPT", anchor: "end", offset: [-12, 8] },
  { id: "PORT_MUNDRA", text: "Mundra", anchor: "end", offset: [-12, 0] },
  { id: "PORT_KOLKATA", text: "Haldia", anchor: "start", offset: [12, 6] },
  { id: "DC_BLR", text: "BLR-Hoskote DC", anchor: "end", offset: [-12, 10] },
  { id: "DC_NAGPUR", text: "Nagpur hub", anchor: "start", offset: [12, 0] },
  { id: "DC_DELHI", text: "Delhi-NCR DC", anchor: "start", offset: [12, 6] },
  { id: "DC_PUNE", text: "Pune DC", anchor: "start", offset: [12, 4] },
  { id: "SUP_SHENZHEN", text: "Shenzhen", anchor: "start", offset: [12, 0] },
];
const withA = (c: [number, number, number], a: number): RGBA => [c[0], c[1], c[2], a];

export function laneStatus(l: Lane, s: NetState): "ok" | "bad" | "ai" | "warn" {
  if (s.scenarioActive && scenario.disrupted_lanes.includes(l.id)) return "bad";
  if (s.mitigated && scenario.reroute_lanes.includes(l.id)) return "ai";
  if ((s.timeOffsetH ?? 0) > 0 && scenario.disrupted_lanes.includes(l.id)) return "warn";
  return "ok";
}

export function nodeStatus(n: NetNode, s: NetState): "ok" | "warn" | "bad" | "good" | "zone" {
  const future = (s.timeOffsetH ?? 0) > 0;
  if (n.id === "PORT_CHENNAI" && (s.scenarioActive || future)) return s.scenarioActive ? "bad" : "warn";
  if (n.id === "DC_HYD_SHAMSHABAD") {
    if (s.mitigated) return "good";
    if (s.scenarioActive || (future && predictedStock(s.timeOffsetH ?? 0) < scenario.safety_stock)) return s.scenarioActive ? "bad" : "warn";
  }
  if (n.type === "zone") return "zone";
  return "ok";
}

/** Predicted P50 vaccine stock at Shamshabad `h` hours ahead (from the Monte Carlo fan). */
export function predictedStock(h: number, mitigated = false): number {
  const band = mitigated ? scenario.mitigated.p50 : scenario.baseline.p50;
  const idx = Math.max(0, Math.min(band.length - 1, Math.round((h / 24) * 4)));
  return band[idx];
}

const statusRGB = { ok: RGB.ok, warn: RGB.warn, bad: RGB.bad, ai: RGB.ai, good: RGB.good, zone: RGB.dim } as const;

export function networkLayers(s: NetState): Layer[] {
  const L = s.layers ?? { nodes: true, lanes: true, trucks: true, ships: true, inventory: true, weather: true, risk: false };
  const off = s.timeOffsetH ?? 0;
  const future = off > 0;
  const ghost = future ? 0.55 : 1;
  const draw = s.drawProgress ?? 1;
  const p = s.idPrefix ?? "";
  const visibleLanes = laneGeo.slice(0, Math.ceil(laneGeo.length * draw));
  const drawnIds = new Set(visibleLanes.map((g) => g.lane.id));
  const key = `${s.scenarioActive}-${s.mitigated}-${future}`;
  const out: Layer[] = [];

  if (L.risk) {
    out.push(new HeatmapLayer({
      id: `${p}risk`,
      data: [...shipments.filter((x) => x.status !== "in_transit"), ...network.nodes.filter((n) => n.type !== "zone")],
      getPosition: (d: (typeof shipments)[number] | NetNode) => {
        if ("lane_id" in d) { const g = geoByLane.get(d.lane_id)!; const q = pointAt(g.path, g.ts, d.progress); return [q[0], q[1]]; }
        return [d.lon, d.lat];
      },
      getWeight: (d: (typeof shipments)[number] | NetNode) => ("lane_id" in d ? 1 : d.betweenness * 20 + (d.id === "PORT_CHENNAI" ? 12 : 0)),
      radiusPixels: 60, intensity: 1.2, threshold: 0.05,
      colorRange: [[34, 211, 238, 0], [34, 211, 238, 60], [245, 158, 11, 120], [245, 158, 11, 170], [239, 68, 68, 200], [239, 68, 68, 230]],
    }));
  }

  if (L.lanes) {
    out.push(new PathLayer<LaneGeo>({
      id: `${p}lanes`,
      data: visibleLanes,
      getPath: (d) => d.path,
      getColor: (d) => {
        const st = laneStatus(d.lane, s);
        return withA(statusRGB[st], (st === "ok" ? 70 : 170) * ghost);
      },
      getWidth: (d) => (laneStatus(d.lane, s) === "ok" ? 1.2 : 2.4),
      widthUnits: "pixels",
      pickable: true,
      updateTriggers: { getColor: [key, off], getWidth: key },
      parameters: { depthCompare: "always" },
    }));
    out.push(new TripsLayer<Particle>({
      id: `${p}flow`,
      data: particles.filter((x) => drawnIds.has(x.laneId) && !(s.scenarioActive && scenario.disrupted_lanes.includes(x.laneId))),
      getPath: (d) => d.path,
      getTimestamps: (d) => d.timestamps,
      getColor: (d) => {
        const st = laneStatus(laneById.get(d.laneId)!, s);
        return statusRGB[st];
      },
      opacity: 0.9 * ghost,
      widthMinPixels: 2.2,
      capRounded: true, jointRounded: true,
      trailLength: 5,
      currentTime: (s.t * 2 + off * 0.5) % LOOP,
      updateTriggers: { getColor: key },
      parameters: { depthCompare: "always" },
    }));
  }

  if (L.trucks) {
    out.push(new ScatterplotLayer<(typeof shipments)[number]>({
      id: `${p}shipments`,
      data: draw < 1 ? [] : shipments,
      getPosition: (d) => {
        const g = geoByLane.get(d.lane_id)!;
        const f = (d.progress + (s.t * 0.004 + off * 0.01) * d.speed) % 1;
        return pointAt(g.path, g.ts, f) as [number, number, number];
      },
      getFillColor: (d) => {
        const disrupted = s.scenarioActive && scenario.disrupted_lanes.includes(d.lane_id);
        if (disrupted) return withA(RGB.bad, 255);
        return d.status === "in_transit" ? withA(RGB.ink, 220 * ghost) : withA(RGB.warn, 240 * ghost);
      },
      getRadius: 2.6,
      radiusUnits: "pixels",
      stroked: true,
      getLineColor: [7, 11, 20, 200],
      lineWidthMinPixels: 0.8,
      pickable: true,
      updateTriggers: { getPosition: [s.t, off], getFillColor: key },
      parameters: { depthCompare: "always" },
    }));
  }

  if (L.inventory) {
    const invNodes = network.nodes.filter((n) => network.inventory[n.id]);
    out.push(new ColumnLayer<NetNode>({
      id: `${p}inventory`,
      data: draw < 1 ? [] : invNodes,
      diskResolution: 12,
      radius: 11000,
      extruded: true,
      getPosition: (d) => [d.lon + 0.18, d.lat - 0.12],
      getElevation: (d) => {
        let total = network.inventory[d.id].reduce((a, r) => a + r.on_hand, 0);
        if (d.id === "DC_HYD_SHAMSHABAD" && (future || s.scenarioActive)) {
          const vax = network.inventory[d.id].find((r) => r.sku === "SKU_VAX")!;
          total += predictedStock(Math.max(off, s.scenarioActive ? 84 : 0), s.mitigated) - vax.on_hand;
        }
        return Math.max(800, total) * 22;
      },
      getFillColor: (d) => {
        const st = nodeStatus(d, s);
        return withA(st === "zone" ? RGB.dim : statusRGB[st], 210 * ghost);
      },
      material: { ambient: 0.5, diffuse: 0.6, shininess: 40, specularColor: [80, 80, 80] },
      pickable: true,
      updateTriggers: { getElevation: [off, key], getFillColor: [off, key] },
      transitions: { getElevation: 500 },
    }));
  }

  if (L.weather) {
    out.push(new ScatterplotLayer<(typeof control.weather)[number]>({
      id: `${p}weather`,
      data: control.weather,
      getPosition: (d) => [d.lon, d.lat],
      getRadius: (d) => d.wind_kmh * 2600 * (d.code === "cyclone" ? 1 + 0.12 * Math.sin(s.t * 2) : 1),
      getFillColor: (d) => (d.code === "cyclone" ? [245, 158, 11, 28] : [154, 167, 189, 12]),
      getLineColor: (d) => (d.code === "cyclone" ? [245, 158, 11, 140] : [154, 167, 189, 50]),
      stroked: true,
      lineWidthMinPixels: 1,
      pickable: true,
      updateTriggers: { getRadius: s.t },
    }));
  }

  if (L.ships) {
    out.push(new ScatterplotLayer<(typeof control.ships)[number]>({
      id: `${p}ships`,
      data: control.ships,
      getPosition: (d) => {
        const h = (d.heading * Math.PI) / 180;
        const k = (s.t * 0.004) % 0.6;
        return [d.lon + Math.sin(h) * k, d.lat + Math.cos(h) * k];
      },
      getRadius: 3, radiusUnits: "pixels",
      getFillColor: [230, 237, 247, 200],
      stroked: true, getLineColor: [34, 211, 238, 120], lineWidthMinPixels: 3,
      pickable: true,
      updateTriggers: { getPosition: s.t },
    }));
  }

  if (L.nodes) {
    const nodes = draw < 1 ? network.nodes.filter((n) => n.type !== "zone") : network.nodes;
    const alarm = nodes.filter((n) => ["bad", "warn"].includes(nodeStatus(n, s)));
    const phase = (s.t % 1.6) / 1.6;
    out.push(new ScatterplotLayer<NetNode>({
      id: `${p}pulse`,
      data: alarm,
      getPosition: (d) => [d.lon, d.lat],
      getRadius: 10 + phase * 26,
      radiusUnits: "pixels",
      stroked: true, filled: false,
      getLineColor: (d) => withA(statusRGB[nodeStatus(d, s)] as [number, number, number], 255 * (1 - phase)),
      lineWidthMinPixels: 2,
      updateTriggers: { getRadius: s.t, getLineColor: [s.t, key, off] },
      parameters: { depthCompare: "always" },
    }));
    out.push(new ScatterplotLayer<NetNode>({
      id: `${p}nodes`,
      data: nodes,
      getPosition: (d) => [d.lon, d.lat],
      getRadius: (d) => ({ port: 7, plant: 6, supplier: 5.5, dc: 7, zone: 3.5 })[d.type] * (s.selectedNode === d.id ? 1.5 : 1),
      radiusUnits: "pixels",
      getFillColor: (d) => {
        const st = nodeStatus(d, s);
        return st === "zone" ? [154, 167, 189, 150] : withA(statusRGB[st], 255);
      },
      stroked: true,
      getLineColor: (d) => (s.selectedNode === d.id ? [255, 255, 255, 255] : [7, 11, 20, 255]),
      lineWidthMinPixels: 2,
      pickable: true,
      updateTriggers: { getFillColor: [key, off], getRadius: s.selectedNode, getLineColor: s.selectedNode },
      parameters: { depthCompare: "always" },
    }));
    if (s.showLabels !== false) {
      out.push(new TextLayer<(typeof LABELS)[number]>({
        id: `${p}labels`,
        data: LABELS,
        getPosition: (d) => [nodeById.get(d.id)!.lon, nodeById.get(d.id)!.lat],
        getText: (d) => d.text,
        getSize: 11.5,
        getColor: [230, 237, 247, 215],
        getPixelOffset: (d) => d.offset,
        getTextAnchor: (d) => d.anchor,
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
  }
  return out;
}

export function cyclonePolygonLayer(t: number, id = "cyclone") {
  const cx = 80.45, cy = 13.3;
  const rot = t * 0.4;
  const poly = scenario.cyclone_polygon.map(([x, y]) => {
    const dx = x - cx, dy = y - cy;
    const k = 1 + 0.04 * Math.sin(t * 2);
    return [cx + k * (dx * Math.cos(rot) - dy * Math.sin(rot)), cy + k * (dx * Math.sin(rot) + dy * Math.cos(rot))];
  });
  return new PolygonLayer({
    id,
    data: [{ polygon: poly }],
    getPolygon: (d: { polygon: number[][] }) => d.polygon,
    getFillColor: [239, 68, 68, 45],
    getLineColor: [239, 68, 68, 220],
    lineWidthMinPixels: 2,
    stroked: true,
    updateTriggers: { getPolygon: t },
  });
}

export function nodeTooltip(n: NetNode) {
  const inv = network.inventory[n.id];
  const invHtml = inv
    ? inv.map((r) => `<div style="display:flex;justify-content:space-between;gap:14px"><span style="color:#9AA7BD">${r.sku.replace("SKU_", "")}</span><span style="font-family:JetBrains Mono Variable,monospace">${r.on_hand.toLocaleString("en-IN")}</span></div>`).join("")
    : "";
  return `<div style="font-weight:600;margin-bottom:4px">${n.name}</div><div style="color:#5D6A82;font-size:11px;margin-bottom:4px">${n.id} · ${n.type}</div>${invHtml}<div style="color:#5D6A82;font-size:11px;margin-top:4px">Click for details</div>`;
}
