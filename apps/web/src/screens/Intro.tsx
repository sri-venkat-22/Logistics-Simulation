import { useEffect, useState } from "react";
import { useNavigate } from "react-router";
import { AnimatePresence, motion } from "motion/react";
import { DeckMap, useAnimationClock } from "../components/DeckMap";
import { Logo } from "../components/Shell";
import { Button, Kbd, MockChip } from "../components/ui";
import { networkLayers } from "../lib/networkLayers";
import { VIEW } from "../lib/theme";

/** §6.5 screen 1 — 5 s skippable intro: globe rotates → flies to India → arcs draw in → tagline. */
export default function Intro() {
  const navigate = useNavigate();
  const t = useAnimationClock();
  const [phase, setPhase] = useState<"globe" | "fly" | "draw" | "tagline">("globe");
  const [drawStart, setDrawStart] = useState<number | null>(null);

  useEffect(() => {
    const skip = (e: KeyboardEvent) => { if (["Enter", " ", "Escape"].includes(e.key)) navigate("/"); };
    window.addEventListener("keydown", skip);
    return () => window.removeEventListener("keydown", skip);
  }, [navigate]);

  const onLoad = (e: { target: maplibregl.Map }) => {
    const m = e.target;
    m.easeTo({ center: [95, 18], duration: 1600, easing: (x) => x });
    window.setTimeout(() => {
      setPhase("fly");
      m.flyTo({ ...{ center: [VIEW.INDIA.longitude, VIEW.INDIA.latitude], zoom: VIEW.INDIA.zoom, pitch: VIEW.INDIA.pitch, bearing: VIEW.INDIA.bearing }, duration: 2800, curve: 1.6, essential: true });
    }, 1600);
    window.setTimeout(() => { setPhase("draw"); setDrawStart(performance.now()); }, 4300);
    window.setTimeout(() => setPhase("tagline"), 6200);
  };

  const draw = drawStart === null ? 0 : Math.min(1, (performance.now() - drawStart) / 1900);
  const layers = phase === "globe" || phase === "fly" ? [] : networkLayers({ t, drawProgress: draw, showLabels: phase === "tagline", idPrefix: "intro-" });

  return (
    <div className="absolute inset-0 bg-bg">
      <DeckMap initialViewState={{ longitude: 30, latitude: 15, zoom: 1.35, pitch: 0, bearing: 0 }} globe interactive={false} layers={layers} onLoad={onLoad} />
      <div className="absolute inset-0 pointer-events-none bg-[radial-gradient(ellipse_at_center,transparent_35%,rgba(7,11,20,0.85))]" />

      <AnimatePresence>
        {(phase === "globe" || phase === "fly") && (
          <motion.div key="hook" initial={{ opacity: 0 }} animate={{ opacity: 1 }} exit={{ opacity: 0 }} transition={{ duration: 0.8 }}
            className="absolute left-1/2 top-[14%] -translate-x-1/2 max-w-[640px] text-center px-6">
            <p className="text-[17px] leading-relaxed text-ink-2">
              In December 2023, Cyclone Michaung flooded Chennai for days.<br />
              Every pharma distributor in Hyderabad found out <em className="text-ink not-italic font-medium">after</em> their shelves were empty.
            </p>
          </motion.div>
        )}
        {phase === "tagline" && (
          <motion.div key="tag" initial={{ opacity: 0, y: 16 }} animate={{ opacity: 1, y: 0 }} transition={{ duration: 0.9, ease: [0.22, 1, 0.36, 1] }}
            className="absolute left-1/2 bottom-[11%] -translate-x-1/2 text-center">
            <div className="flex items-center justify-center gap-3">
              <Logo size={40} />
              <span className="text-[34px] font-semibold tracking-tight">AEGIS <span className="text-ok">Twin</span></span>
            </div>
            <p className="mt-3 text-[18px] text-ink-2 tracking-tight">See every shipment. Simulate every shock. Survive every attack.</p>
            <div className="mt-6 flex items-center justify-center gap-3 pointer-events-auto">
              <Button variant="primary" className="h-10 px-5 text-[14px]" onClick={() => navigate("/")}>Enter Control Tower</Button>
              <span className="text-[12px] text-ink-3 flex items-center gap-1.5"><Kbd>↵</Kbd> or <Kbd>esc</Kbd></span>
            </div>
          </motion.div>
        )}
      </AnimatePresence>

      <div className="absolute top-4 left-4"><MockChip /></div>
      <button onClick={() => navigate("/")} className="absolute top-4 right-4 text-[12px] text-ink-3 hover:text-ink cursor-pointer px-3 h-8 rounded-lg border border-line">Skip intro</button>
    </div>
  );
}
