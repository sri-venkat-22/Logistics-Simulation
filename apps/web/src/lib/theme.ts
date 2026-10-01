import type { Mode, NodeType, Sev } from "./data";

type RGB = [number, number, number];

/** Semantic colours (§6.5). Colour carries meaning only. */
export const C = {
  bg: "#070B14",
  ok: "#22D3EE",
  warn: "#F59E0B",
  bad: "#EF4444",
  ai: "#A78BFA",
  good: "#34D399",
  ink: "#E6EDF7",
  ink2: "#9AA7BD",
  ink3: "#5D6A82",
  line: "rgba(255,255,255,0.08)",
} as const;

export const RGB: Record<"ok" | "warn" | "bad" | "ai" | "good" | "ink" | "dim", RGB> = {
  ok: [34, 211, 238],
  warn: [245, 158, 11],
  bad: [239, 68, 68],
  ai: [167, 139, 250],
  good: [52, 211, 153],
  ink: [230, 237, 247],
  dim: [93, 106, 130],
};

export const sevColor: Record<Sev, string> = {
  ok: C.ok, warn: C.warn, bad: C.bad, ai: C.ai, good: C.good, sec: C.bad,
};

/** Node types are distinguished by shape/size + label, not hue; hue is reserved for status. */
export const nodeRadius: Record<NodeType, number> = { port: 9, plant: 8, supplier: 7, dc: 9, zone: 4 };
export const nodeTypeLabel: Record<NodeType, string> = {
  port: "Port", plant: "Plant", supplier: "Supplier", dc: "Distribution centre", zone: "Demand zone",
};
export const modeLabel: Record<Mode, string> = { road: "Road", rail: "Rail", sea: "Sea", air: "Air" };

export const SATELLITE = {
  version: 8,
  sources: {
    satellite: {
      type: "raster",
      tiles: ["https://server.arcgisonline.com/ArcGIS/rest/services/World_Imagery/MapServer/tile/{z}/{y}/{x}"],
      tileSize: 256,
      maxzoom: 19,
    },
  },
  layers: [
    {
      id: "background",
      type: "background",
      paint: { "background-color": "rgba(0,0,0,0)" }
    },
    {
      id: "satellite",
      type: "raster",
      source: "satellite",
      minzoom: 0,
      maxzoom: 19,
    },
  ],
};
export const OFM_DARK = "https://tiles.openfreemap.org/styles/dark";

export const VIEW = {
  WORLD: { longitude: 60, latitude: 18, zoom: 1.2, pitch: 0, bearing: 0 },
  INDIA: { longitude: 80.6, latitude: 19.6, zoom: 4.25, pitch: 35, bearing: -6 },
  HYDERABAD: { longitude: 78.45, latitude: 17.43, zoom: 10.4, pitch: 58, bearing: -18 },
} as const;

/** Shared ECharts base theme so every chart reads as one system. */
export const chartBase = {
  backgroundColor: "transparent",
  textStyle: { fontFamily: "Geist Variable, Inter, sans-serif", color: C.ink2, fontSize: 11 },
  grid: { left: 44, right: 16, top: 24, bottom: 28, containLabel: false },
  tooltip: {
    trigger: "axis" as const,
    backgroundColor: "rgba(15,22,38,0.95)",
    borderColor: "rgba(255,255,255,0.12)",
    textStyle: { color: C.ink, fontSize: 12 },
    axisPointer: { lineStyle: { color: "rgba(255,255,255,0.2)" } },
  },
};
export const axis = {
  axisLine: { lineStyle: { color: "rgba(255,255,255,0.12)" } },
  axisTick: { show: false },
  axisLabel: { color: C.ink3, fontFamily: "JetBrains Mono Variable, monospace", fontSize: 10 },
  splitLine: { lineStyle: { color: "rgba(255,255,255,0.05)" } },
};
