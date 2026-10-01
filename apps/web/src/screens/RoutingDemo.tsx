import { useMemo, useState } from "react";
import { PathLayer, ScatterplotLayer, TextLayer } from "@deck.gl/layers";
import { ScenegraphLayer } from "@deck.gl/mesh-layers";
import { DeckMap, useAnimationClock } from "../components/DeckMap";
import { network, nodeById, type NetNode } from "../lib/data";
import { laneGeo } from "../lib/networkLayers";
import { VIEW } from "../lib/theme";
import { findBestRoute } from "../lib/routing";

export default function RoutingDemo() {
  const [start, setStart] = useState("DC_DELHI");
  const [end, setEnd] = useState("PORT_CHENNAI");
  const [disrupted, setDisrupted] = useState("");
  const [simulating, setSimulating] = useState(false);
  const [fetchingRoads, setFetchingRoads] = useState(false);
  const [route, setRoute] = useState<{ laneIds: string[], nodes: NetNode[] } | null>(null);
  const [roadPath, setRoadPath] = useState<[number, number][] | null>(null);
  
  const [spectate, setSpectate] = useState(false);
  const [vs, setVs] = useState<any>(VIEW.INDIA);
  
  const t = useAnimationClock(simulating);
  const T_MAX = spectate ? 45000 : 45; // 1000x slower in spectate to allow map tiles to stream in

  let truckPos: [number, number, number] | null = null;
  let truckAngle = 0;

  if (simulating && roadPath && roadPath.length > 1) {
    const progress = Math.max(0, (t % T_MAX) / T_MAX);
    const exactIndex = Math.max(0, progress * (roadPath.length - 1));
    const i0 = Math.floor(exactIndex);
    const i1 = Math.min(i0 + 1, roadPath.length - 1);
    const f = exactIndex - i0;
    
    const p0 = roadPath[i0];
    const p1 = roadPath[i1];
    
    if (p0 && p1) {
      truckPos = [ p0[0] + (p1[0] - p0[0]) * f, p0[1] + (p1[1] - p0[1]) * f, 0 ];
    }

    const lookAhead = Math.min(i0 + 3, roadPath.length - 1);
    const pAhead = roadPath[lookAhead];
    if (pAhead) {
      const dx = pAhead[0] - p0[0];
      const dy = pAhead[1] - p0[1];
      truckAngle = Math.atan2(dx, dy) * 180 / Math.PI;
    }
  }

  let activeVs = vs;
  if (spectate && truckPos) {
    activeVs = {
      ...vs,
      longitude: truckPos[0],
      latitude: truckPos[1],
      zoom: 18.5, // Ultra-close zoom for driver POV
      pitch: 85, // Look straight ahead towards the horizon
      bearing: truckAngle
    };
  }

  const layers = useMemo(() => {
    const out: any[] = [];
    
    out.push(new ScatterplotLayer({
      id: "all-nodes", data: network.nodes.filter(n => n.type !== "zone"),
      getPosition: (d: any) => [d.lon, d.lat], getRadius: 4, radiusUnits: "pixels",
      getFillColor: [100, 100, 100, 100], stroked: true, getLineColor: [200, 200, 200, 100],
    }));

    out.push(new TextLayer({
      id: "all-node-names", data: network.nodes.filter(n => n.type !== "zone"),
      getPosition: (d: any) => [d.lon, d.lat], getText: (d: any) => d.name.split(" (")[0],
      getSize: 11, sizeUnits: "pixels", getColor: [220, 220, 220, 255],
      getPixelOffset: [0, -12], fontFamily: "Inter, sans-serif",
      background: true, backgroundColor: [0, 0, 0, 150], backgroundPadding: [4, 2],
      parameters: { depthTest: false }
    }));

    if (disrupted) {
      const dNode = nodeById.get(disrupted);
      if (dNode) {
        out.push(new ScatterplotLayer({
          id: "disrupted-node", data: [dNode], getPosition: d => [d.lon, d.lat],
          getRadius: 12, radiusUnits: "pixels", getFillColor: [239, 68, 68, 255],
          stroked: true, getLineColor: [255, 255, 255, 255], lineWidthMinPixels: 2, parameters: { depthTest: false }
        }));
      }
    }

    if (route && route.nodes.length > 0) {
      out.push(new ScatterplotLayer({
        id: "route-nodes", data: route.nodes, getPosition: d => [d.lon, d.lat],
        getRadius: 8, radiusUnits: "pixels", getFillColor: [34, 211, 238, 255],
        stroked: true, getLineColor: [255, 255, 255, 255], lineWidthMinPixels: 2, parameters: { depthTest: false }
      }));

      out.push(new TextLayer({
        id: "route-node-names", data: route.nodes, getPosition: d => [d.lon, d.lat],
        getText: d => d.name.split(" (")[0], getSize: 16, sizeUnits: "pixels",
        getColor: [255, 255, 255, 255], getPixelOffset: [0, -20],
        fontFamily: "Inter, sans-serif", fontWeight: 700,
        background: true, backgroundColor: [34, 211, 238, 50], backgroundPadding: [6, 4],
        parameters: { depthTest: false }
      }));
    }

    if (roadPath && roadPath.length > 0) {
      out.push(new PathLayer({
        id: "best-route-path",
        data: [{ path: roadPath }],
        getPath: d => d.path,
        getColor: [34, 211, 238, 220],
        getWidth: 4, widthUnits: "pixels",
        parameters: { depthTest: false }
      }));
    } else if (route && route.laneIds && route.laneIds.length > 0 && !fetchingRoads) {
      const geoList = route.laneIds.map(id => laneGeo.find(g => g.lane.id === id)).filter(Boolean) as typeof laneGeo;
      out.push(new PathLayer({
        id: "best-route-path",
        data: geoList,
        getPath: d => d.path,
        getColor: [34, 211, 238, 200],
        getWidth: 3, widthUnits: "pixels",
        parameters: { depthTest: false }
      }));
    }

    if (truckPos) {
      out.push(new ScenegraphLayer({
        id: "active-truck-3d",
        data: [truckPos],
        scenegraph: "https://raw.githubusercontent.com/KhronosGroup/glTF-Sample-Models/master/2.0/CesiumMilkTruck/glTF-Binary/CesiumMilkTruck.glb",
        getPosition: d => d,
        getOrientation: [0, -truckAngle, 90], 
        sizeScale: 15000,
        _lighting: "pbr",
        parameters: { depthTest: false }
      }));

      out.push(new TextLayer({
        id: "truck-pointer",
        data: [truckPos],
        getPosition: d => d,
        getText: () => "⬇ TRUCK",
        getSize: 16, sizeUnits: "pixels",
        getColor: [239, 68, 68, 255],
        getPixelOffset: [0, -45],
        fontFamily: "Inter, sans-serif", fontWeight: 800,
        background: true, backgroundColor: [255, 255, 255, 200], backgroundPadding: [4, 2],
        parameters: { depthTest: false }
      }));
    }

    return out;
  }, [route, roadPath, disrupted, fetchingRoads, truckPos, truckAngle]);

  const handleStartNavigation = async () => {
    const bestRoute = findBestRoute(start, end, disrupted ? [disrupted] : []);
    setRoute(bestRoute);
    setRoadPath(null);
    setSimulating(false);
    setSpectate(false);
    
    if (bestRoute.nodes.length < 2) return;
    
    setFetchingRoads(true);
    try {
      const coords = bestRoute.nodes.map(n => `${n.lon.toFixed(5)},${n.lat.toFixed(5)}`).join(";");
      const url = `https://router.project-osrm.org/route/v1/driving/${coords}?overview=full&geometries=geojson`;
      const res = await fetch(url);
      const data = await res.json();
      if (data.routes && data.routes.length > 0) {
        setRoadPath(data.routes[0].geometry.coordinates);
        setSimulating(true);
      }
    } catch (err) {
      console.error("OSRM Routing failed", err);
    }
    setFetchingRoads(false);
  };

  return (
    <div className="absolute inset-0">
      <DeckMap layers={layers} viewState={activeVs} onMove={v => { setVs(v); if (spectate) setSpectate(false); }} globe={true} />
      
      <div className="absolute top-20 left-10 w-[340px] glass p-5 rounded-xl border border-line shadow-2xl z-10 flex flex-col gap-4">
        <div>
          <h2 className="text-lg font-semibold text-ink">Live Navigation System</h2>
          <p className="text-xs text-ink-2 mt-1">Select an origin, destination, and inject disruptions to see the AI dynamically calculate precise real-world road navigation.</p>
        </div>

        <div className="flex flex-col gap-3">
          <label className="flex flex-col gap-1.5">
            <span className="text-[11px] font-semibold tracking-wider text-ink-3 uppercase">Origin</span>
            <select className="bg-bg border border-line rounded-lg px-3 py-2 text-sm text-ink outline-none" value={start} onChange={e => setStart(e.target.value)}>
              {network.nodes.filter(n => n.type !== "zone").map(n => (
                <option key={n.id} value={n.id}>{n.name}</option>
              ))}
            </select>
          </label>
          <label className="flex flex-col gap-1.5">
            <span className="text-[11px] font-semibold tracking-wider text-ink-3 uppercase">Destination</span>
            <select className="bg-bg border border-line rounded-lg px-3 py-2 text-sm text-ink outline-none" value={end} onChange={e => setEnd(e.target.value)}>
              {network.nodes.filter(n => n.type !== "zone").map(n => (
                <option key={n.id} value={n.id}>{n.name}</option>
              ))}
            </select>
          </label>
          <label className="flex flex-col gap-1.5">
            <span className="text-[11px] font-semibold tracking-wider text-bad uppercase">Inject Disruption (Close Node)</span>
            <select className="bg-bad/10 border border-bad/30 rounded-lg px-3 py-2 text-sm text-bad outline-none" value={disrupted} onChange={e => setDisrupted(e.target.value)}>
              <option value="">None (Normal Operations)</option>
              {network.nodes.filter(n => n.type !== "zone").map(n => (
                <option key={n.id} value={n.id}>{n.name}</option>
              ))}
            </select>
          </label>
        </div>

        <div className="flex gap-2 mt-2">
          <button 
            onClick={handleStartNavigation} 
            disabled={fetchingRoads}
            className="flex-1 bg-ai/20 hover:bg-ai/30 text-ai border border-ai/40 rounded-lg py-2.5 font-medium transition cursor-pointer disabled:opacity-50"
          >
            {fetchingRoads ? "Calculating Routes..." : "Start Navigation"}
          </button>
          
          {simulating && (
            <button 
              onClick={() => setSpectate(!spectate)} 
              className={`px-4 font-medium rounded-lg border transition ${spectate ? 'bg-ok text-bg border-ok' : 'bg-bg text-ink border-line hover:bg-line'}`}
            >
              {spectate ? "Stop Spectating" : "Spectate 🎥"}
            </button>
          )}
        </div>
      </div>
    </div>
  );
}
