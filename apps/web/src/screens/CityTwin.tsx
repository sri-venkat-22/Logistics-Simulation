import { useEffect, useMemo, useRef, useState } from "react";
import { toast } from "sonner";
import clsx from "clsx";
import { Marker } from "react-map-gl/maplibre";
import { PathLayer, ScatterplotLayer, TextLayer } from "@deck.gl/layers";
import { TripsLayer } from "@deck.gl/geo-layers";
import { PathStyleExtension } from "@deck.gl/extensions";
import type { PickingInfo } from "@deck.gl/core";
import { Waves, RotateCcw, Truck, Car, Timer, Cpu, Sparkles } from "lucide-react";
import { DeckMap, useAnimationClock, tooltipStyle } from "../components/DeckMap";
import { Badge, Button, Eyebrow, Glass, Ticker } from "../components/ui";
import { hyderabad, type Route } from "../lib/data";
import { normalisedTimestamps, pointAt, rng, type LonLat } from "../lib/geo";
import { useAegis } from "../lib/store";
import { OFM_DARK, VIEW } from "../lib/theme";

const LOOP = 3600;        // sim-seconds per loop
const SPEEDUP = 60;       // 1 wall-second = 1 sim-minute (demo mode)
const FLOOD = hyderabad.flood_route_index;

interface RouteGeo { idx: number; route: Route; ts: number[] }
const routeGeo: RouteGeo[] = hyderabad.routes.map((route, idx) => ({ idx, route, ts: normalisedTimestamps(route.path) }));
const detourGeo = { route: hyderabad.detour, ts: normalisedTimestamps(hyderabad.detour.path) };

interface TruckTrip { id: string; routeIdx: number; path: LonLat[]; timestamps: number[] }

function buildTrucks(n: number, closed: Set<number>): TruckTrip[] {
  const r = rng(11);
  const out: TruckTrip[] = [];
  for (let i = 0; i < n; i++) {
    // the ORR corridor carries more freight
    const routeIdx = r() < 0.18 ? FLOOD : Math.floor(r() * routeGeo.length);
    const reversed = r() < 0.5;
    const off = r() * LOOP;
    const speed = 0.85 + r() * 0.3;
    let g = routeGeo[routeIdx] as { route: Route; ts: number[] };
    if (closed.has(routeIdx)) {
      if (routeIdx === FLOOD) g = detourGeo; else continue; // only the flood corridor has a precomputed detour
    }
    const path = reversed ? [...g.route.path].reverse() : g.route.path;
    const ts = reversed ? [...g.ts].reverse().map((x) => 1 - x) : g.ts;
    const dur = g.route.duration_s * 1.35 / speed; // trucks are slower than OSRM car profile
    for (const shift of [0, -LOOP]) out.push({ id: `T${i}`, routeIdx, path, timestamps: ts.map((x) => off + shift + x * dur) });
  }
  return out;
}

interface CarSeed { g: RouteGeo; off: number; speed: number; rev: boolean }
function buildCars(n: number): CarSeed[] {
  const r = rng(99);
  return Array.from({ length: n }, () => ({ g: routeGeo[Math.floor(r() * routeGeo.length)], off: r(), speed: 0.6 + r() * 0.9, rev: r() < 0.5 }));
}

function useFps() {
  const [fps, setFps] = useState(60);
  useEffect(() => {
    let frames = 0, last = performance.now(), raf = 0;
    const loop = (t: number) => {
      frames++;
      if (t - last >= 500) { setFps(Math.round((frames * 1000) / (t - last))); frames = 0; last = t; }
      raf = requestAnimationFrame(loop);
    };
    raf = requestAnimationFrame(loop);
    return () => cancelAnimationFrame(raf);
  }, []);
  return fps;
}

export default function CityTwin() {
  const t = useAnimationClock();
  const fps = useFps();
  const { roadClosed, setRoadClosed, appliedPlan } = useAegis();
  const [stress, setStress] = useState(false);
  const [closed, setClosed] = useState<Set<number>>(new Set(roadClosed ? [FLOOD] : []));
  const closedAt = useRef(0);

  useEffect(() => {
    setClosed((prev) => {
      const next = new Set(prev);
      if (roadClosed) next.add(FLOOD); else next.delete(FLOOD);
      return next;
    });
    if (roadClosed) closedAt.current = performance.now();
  }, [roadClosed]);

  const nTrucks = stress ? 2000 : 420;
  const nCars = stress ? 3000 : 1200;
  const trucks = useMemo(() => buildTrucks(nTrucks, closed), [nTrucks, closed]);
  const cars = useMemo(() => buildCars(nCars), [nCars]);
  const simT = (t * SPEEDUP) % LOOP;
  const floodClosed = closed.has(FLOOD);
  const planA = appliedPlan === "A";

  const onLoad = (e: { target: maplibregl.Map }) => {
    const m = e.target;
    if (m.getLayer("aegis-buildings-3d")) return;
    const firstSymbol = m.getStyle().layers.find((l) => l.type === "symbol")?.id;
    m.addLayer({
      id: "aegis-buildings-3d", type: "fill-extrusion", source: "openmaptiles", "source-layer": "building", minzoom: 12,
      paint: {
        "fill-extrusion-color": ["interpolate", ["linear"], ["coalesce", ["get", "render_height"], 6], 0, "#111a2b", 40, "#1b2740", 120, "#26365a"],
        "fill-extrusion-height": ["coalesce", ["get", "render_height"], 6],
        "fill-extrusion-base": ["coalesce", ["get", "render_min_height"], 0],
        "fill-extrusion-opacity": 0.88,
      },
    }, firstSymbol);
  };

  const toggleRoute = (idx: number) => {
    if (idx === FLOOD) { setRoadClosed(!floodClosed); if (!floodClosed) toast.error("ORR corridor flooded · Medchal → Shamshabad", { description: `${Math.round(trucks.filter((x) => x.routeIdx === FLOOD).length / 2)} trucks rerouting via Uppal / LB Nagar (+${Math.round((hyderabad.detour.duration_s - hyderabad.routes[FLOOD].duration_s) * 1.35 / 60)} min through city streets)` }); return; }
    setClosed((prev) => { const n = new Set(prev); if (n.has(idx)) n.delete(idx); else n.add(idx); return n; });
    toast(`${hyderabad.routes[idx].from} → ${hyderabad.routes[idx].to} ${closed.has(idx) ? "reopened" : "closed"}`, { description: "Level-1 prototype: rerouting geometry is precomputed only for the ORR flood corridor." });
  };

  const layers = [
    new PathLayer<RouteGeo>({
      id: "roads",
      data: routeGeo,
      getPath: (d) => d.route.path,
      getColor: (d) => (closed.has(d.idx) ? [239, 68, 68, 230] : [34, 211, 238, 38]),
      getWidth: (d) => (closed.has(d.idx) ? 5 : 7),
      widthUnits: "pixels",
      pickable: true,
      autoHighlight: true,
      highlightColor: [255, 255, 255, 50],
      // PathStyleExtension props (not in PathLayer's own prop types)
      ...({ getDashArray: (d: RouteGeo) => (closed.has(d.idx) ? [3, 2] : [0, 0]), dashJustified: true } as object),
      extensions: [new PathStyleExtension({ dash: true })],
      updateTriggers: { getColor: [...closed].join(), getWidth: [...closed].join(), getDashArray: [...closed].join() },
    }),
    ...(floodClosed || planA ? [new PathLayer({
      id: "detour",
      data: [detourGeo],
      getPath: (d: typeof detourGeo) => d.route.path,
      getColor: floodClosed ? [52, 211, 153, 210] : [167, 139, 250, 200],
      getWidth: 5, widthUnits: "pixels",
    })] : []),
    new ScatterplotLayer<CarSeed>({
      id: "cars",
      data: cars,
      getPosition: (c) => {
        const f = (c.off + t * 0.004 * c.speed) % 1;
        const p = pointAt(c.g.route.path, c.g.ts, c.rev ? 1 - f : f);
        return [p[0], p[1]];
      },
      getRadius: 1.6, radiusUnits: "pixels",
      getFillColor: [154, 167, 189, 90],
      updateTriggers: { getPosition: t },
    }),
    new TripsLayer<TruckTrip>({
      id: "trucks",
      data: trucks,
      getPath: (d) => d.path,
      getTimestamps: (d) => d.timestamps,
      getColor: (d) => (d.routeIdx === FLOOD && floodClosed ? [52, 211, 153] : [34, 211, 238]),
      widthMinPixels: 3.2,
      trailLength: 240,
      currentTime: simT,
      capRounded: true, jointRounded: true,
      fadeTrail: true,
      updateTriggers: { getColor: floodClosed },
    }),
    new ScatterplotLayer({
      id: "hubs",
      data: hyderabad.hubs,
      getPosition: (d: (typeof hyderabad.hubs)[number]) => [d.lon, d.lat],
      getRadius: 5, radiusUnits: "pixels",
      getFillColor: [230, 237, 247, 255],
      stroked: true, getLineColor: [7, 11, 20, 255], lineWidthMinPixels: 2,
      parameters: { depthCompare: "always" },
    }),
    new TextLayer({
      id: "hub-labels",
      data: hyderabad.hubs,
      getPosition: (d: (typeof hyderabad.hubs)[number]) => [d.lon, d.lat],
      getText: (d: (typeof hyderabad.hubs)[number]) => d.name,
      getSize: 12, getColor: [230, 237, 247, 220],
      getPixelOffset: [0, -16],
      fontFamily: "Geist Variable, Inter, sans-serif", fontWeight: 600,
      outlineWidth: 3, outlineColor: [7, 11, 20, 255], fontSettings: { sdf: true },
      billboard: true,
      parameters: { depthCompare: "always" },
    }),
  ];

  const minutes = Math.floor(simT / 60);
  const clock = `${String(6 + Math.floor(minutes / 60)).padStart(2, "0")}:${String(minutes % 60).padStart(2, "0")}`;
  const corridorBase = hyderabad.routes[FLOOD].duration_s / 60 * 1.35;
  const corridorNow = floodClosed ? hyderabad.detour.duration_s / 60 * 1.35 : corridorBase;

  return (
    <div className="absolute inset-0">
      <DeckMap
        initialViewState={VIEW.HYDERABAD}
        mapStyle={OFM_DARK}
        layers={layers}
        onLoad={onLoad}
        onClick={(info: PickingInfo) => { if (info.layer?.id === "roads" && info.object) toggleRoute((info.object as RouteGeo).idx); }}
        getTooltip={(info) => (info.layer?.id === "roads" && info.object
          ? { html: `<b>${(info.object as RouteGeo).route.from} → ${(info.object as RouteGeo).route.to}</b><div style="font-family:JetBrains Mono Variable,monospace;color:#9AA7BD">${((info.object as RouteGeo).route.distance_m / 1000).toFixed(1)} km</div><div style="color:#5D6A82;font-size:11px">Click to ${closed.has((info.object as RouteGeo).idx) ? "reopen" : "close"} road</div>`, style: tooltipStyle }
          : null)}
      >
        {floodClosed && (
          <Marker longitude={hyderabad.flood_point[0]} latitude={hyderabad.flood_point[1]} anchor="center">
            <div className="relative grid place-items-center w-10 h-10 rounded-full bg-bad/25 border border-bad text-bad pulse-dot"><Waves size={18} className="relative z-10 text-white" /></div>
          </Marker>
        )}
      </DeckMap>

      <Glass className="absolute left-4 top-14 w-[292px] z-10 p-4">
        <Eyebrow>Micro twin · Eclipse SUMO</Eyebrow>
        <div className="mt-1 text-[15px] font-semibold">Hyderabad logistics belt</div>
        <div className="text-[11.5px] text-ink-3 mt-0.5">Medchal ↔ ORR ↔ Shamshabad ↔ Patancheru</div>
        <div className="mt-4 grid grid-cols-2 gap-3">
          {[
            [Truck, "Trucks", nTrucks, ""], [Car, "Cars", nCars, ""],
            [Timer, "Sim clock", null, clock], [Cpu, "Frame rate", fps, " fps"],
          ].map(([Icon, label, v, suf]) => {
            const I = Icon as typeof Truck;
            return (
              <div key={label as string} className="rounded-xl bg-black/20 border border-line px-3 py-2">
                <div className="flex items-center gap-1.5 text-[10.5px] text-ink-3"><I size={12} />{label as string}</div>
                <div className={clsx("num text-[17px] mt-0.5", label === "Frame rate" && (v as number) < 50 ? "text-warn" : "text-ink")}>
                  {v === null ? suf as string : <><Ticker value={v as number} duration={300} />{suf as string}</>}
                </div>
              </div>
            );
          })}
        </div>
        <div className="mt-4">
          <div className="flex justify-between text-[11.5px]"><span className="text-ink-3">Medchal → Shamshabad</span><span className={clsx("num", floodClosed ? "text-warn" : "text-ink")}>{corridorNow.toFixed(0)} min</span></div>
          <div className="text-[10.5px] text-ink-3 mt-0.5">{floodClosed ? `+${(corridorNow - corridorBase).toFixed(0)} min via detour · feeds macro lane lead time` : "SUMO edge travel times → SimPy lane calibration"}</div>
        </div>
        <div className="mt-4 flex gap-2">
          <Button variant={floodClosed ? "solid" : "danger"} className="flex-1" onClick={() => toggleRoute(FLOOD)}>
            {floodClosed ? <><RotateCcw size={13} /> Reopen ORR</> : <><Waves size={13} /> Flood ORR corridor</>}
          </Button>
          <Button onClick={() => setStress(!stress)} title="NFR: 60 fps with 2,000 vehicles">{stress ? "Normal" : "2k stress"}</Button>
        </div>
        <div className="mt-4 pt-3 border-t border-line space-y-1.5 text-[11px] text-ink-3">
          <div className="flex items-center gap-2"><span className="w-5 h-[3px] rounded bg-ok" /> Truck trail (TripsLayer)</div>
          <div className="flex items-center gap-2"><span className="w-5 h-0 border-t-2 border-dashed border-bad" /> Closed road (old path)</div>
          <div className="flex items-center gap-2"><span className="w-5 h-[3px] rounded bg-good" /> Rerouted path</div>
          <div className="text-[10.5px] pt-1">Click any corridor to close it.</div>
        </div>
      </Glass>

      {(planA || floodClosed) && (
        <div className="absolute right-4 top-14 z-10 glass px-3.5 py-2.5 max-w-[320px]">
          {floodClosed ? (
            <><Badge sev="bad">Road flood</Badge><div className="mt-1.5 text-[12.5px]">ORR Exit 16 closed. <span className="text-good">Trucks rerouted</span> via Uppal / LB Nagar with <span className="num">rerouteTraveltime</span>.</div></>
          ) : (
            <><Badge sev="ai"><Sparkles size={10} /> Plan A live</Badge><div className="mt-1.5 text-[12.5px]">Vizag inbound enters via the eastern ORR (violet). BLR transfer ETA 14 h.</div></>
          )}
        </div>
      )}
      <div className="absolute right-4 bottom-4 z-10 num text-[10.5px] text-ink-3 glass px-2.5 py-1.5">SUMO 1.27 · libsumo · step 1 s · ×{SPEEDUP} · roads © OSM via OSRM</div>
    </div>
  );
}
