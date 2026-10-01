import { forwardRef, useEffect, useImperativeHandle, useRef, useState, type ReactNode } from "react";
import { Map, useControl, type MapRef, type ViewState } from "react-map-gl/maplibre";
import { MapboxOverlay, type MapboxOverlayProps } from "@deck.gl/mapbox";
import type { PickingInfo } from "@deck.gl/core";
import { SATELLITE } from "../lib/theme";

function DeckGLOverlay(props: MapboxOverlayProps) {
  const overlay = useControl<MapboxOverlay>(() => new MapboxOverlay(props));
  overlay.setProps(props);
  return null;
}

export interface DeckMapProps {
  initialViewState?: Partial<ViewState>;
  viewState?: Partial<ViewState>;
  onMove?: (vs: ViewState) => void;
  layers: MapboxOverlayProps["layers"];
  mapStyle?: any;
  globe?: boolean;
  interactive?: boolean;
  onClick?: (info: PickingInfo) => void;
  getTooltip?: MapboxOverlayProps["getTooltip"];
  onLoad?: (e: { target: maplibregl.Map }) => void;
  children?: ReactNode;
  className?: string;
}

/** MapLibre basemap + deck.gl overlay, interleaved so deck layers sit under labels and work on the globe. */
export const DeckMap = forwardRef<MapRef, DeckMapProps>(function DeckMap(
  { initialViewState, viewState, onMove, layers, mapStyle = SATELLITE, globe = false, interactive = true, onClick, getTooltip, onLoad, children, className },
  ref,
) {
  const [loaded, setLoaded] = useState(false);
  const inner = useRef<MapRef>(null);
  useImperativeHandle(ref, () => inner.current!, [loaded]);
  useEffect(() => {
    const t = setTimeout(() => setLoaded(true), 2500); // never leave a blank screen if tiles are slow
    return () => clearTimeout(t);
  }, []);
  return (
    <div 
      className={className ?? "absolute inset-0"} 
      style={{ backgroundImage: "url('/stars.jpg')", backgroundSize: "cover", backgroundPosition: "center" }}
    >
      <Map
        ref={inner}
        initialViewState={initialViewState}
        {...(viewState ? { ...viewState, onMove: (e) => onMove?.(e.viewState) } : {})}
        mapStyle={mapStyle}
        projection={globe ? "globe" : "mercator"}
        interactive={interactive}
        attributionControl={{ compact: true }}
        maxPitch={85}
        onLoad={(e) => { setLoaded(true); onLoad?.(e as unknown as { target: maplibregl.Map }); }}
        style={{ width: "100%", height: "100%", background: "transparent" }}
      >
        <DeckGLOverlay
          layers={layers} interleaved={false} onClick={onClick} getTooltip={getTooltip} pickingRadius={6}
          onError={(e, layer) => { console.error(`[deck] ${layer?.id ?? "deck"}: ${e?.message ?? e}`); return true; }}
          onHover={(info) => {
            const canvas = inner.current?.getCanvas();
            if (canvas) canvas.style.cursor = info.object ? "pointer" : "";
          }}
        />
        {children}
      </Map>
      {!loaded && <div className="absolute inset-0 skeleton rounded-none pointer-events-none" />}
    </div>
  );
});

export const tooltipStyle = {
  backgroundColor: "rgba(15,22,38,0.96)",
  color: "#E6EDF7",
  border: "1px solid rgba(255,255,255,0.12)",
  borderRadius: "10px",
  padding: "8px 10px",
  fontSize: "12px",
  fontFamily: "Geist Variable, Inter, sans-serif",
  boxShadow: "0 8px 30px rgba(0,0,0,0.4)",
};

/** Seconds since mount, updated every animation frame. */
export function useAnimationClock(running = true) {
  const [t, setT] = useState(0);
  useEffect(() => {
    if (!running) return;
    let raf = 0;
    const t0 = performance.now();
    const loop = (now: number) => {
      setT((now - t0) / 1000);
      raf = requestAnimationFrame(loop);
    };
    raf = requestAnimationFrame(loop);
    return () => cancelAnimationFrame(raf);
  }, [running]);
  return t;
}
