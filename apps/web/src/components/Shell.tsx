import { useEffect, useRef, useState, type ReactNode } from "react";
import { NavLink, useLocation, useNavigate } from "react-router";
import { Sparkles, Command, Clapperboard, Square } from "lucide-react";
import { toast } from "sonner";
import clsx from "clsx";
import { SCREENS } from "../lib/screens";
import { useAegis } from "../lib/store";
import { useLive } from "../lib/live";
import { Kbd, MockChip, Ticker } from "./ui";
import { CommandPalette } from "./CommandPalette";
import { CopilotDrawer } from "./CopilotDrawer";

export function Logo({ size = 28 }: { size?: number }) {
  return (
    <svg width={size} height={size} viewBox="0 0 32 32" aria-label="AEGIS Twin">
      <path d="M16 3.5 26.5 7.8v7.4c0 6.7-4.4 11.3-10.5 13.3C9.9 26.5 5.5 21.9 5.5 15.2V7.8z" fill="rgba(34,211,238,0.08)" stroke="#22D3EE" strokeWidth="1.8" strokeLinejoin="round" />
      <circle cx="16" cy="15.2" r="3" fill="#22D3EE" />
      <path d="M16 9.2v2.6M16 18.6v2.6M10 15.2h2.6M19.4 15.2H22" stroke="#22D3EE" strokeWidth="1.4" strokeLinecap="round" opacity="0.7" />
    </svg>
  );
}

function Clock() {
  const [d, setD] = useState(new Date());
  useEffect(() => { const i = setInterval(() => setD(new Date()), 1000); return () => clearInterval(i); }, []);
  return (
    <span className="num text-[12px] text-ink-2">
      {d.toLocaleTimeString("en-IN", { hour12: false, timeZone: "Asia/Kolkata" })} <span className="text-ink-3">IST</span>
    </span>
  );
}

function IngestRate() {
  const live = useLive((s) => s.status === "live");
  const rate = useLive((s) => s.kpis?.ingest_rate ?? 0);
  const [mock, setMock] = useState(5210);
  useEffect(() => {
    if (live) return;
    const i = setInterval(() => setMock(5000 + Math.round(Math.random() * 600)), 1500);
    return () => clearInterval(i);
  }, [live]);
  return (
    <span className="flex items-center gap-1.5 text-[12px] text-ink-2" title={live ? "Measured ingest rate (accepted msgs/s, last 10 s)" : "Mock value"}>
      <span className="inline-block w-1.5 h-1.5 rounded-full bg-ok pulse-dot text-ok" />
      <Ticker value={live ? Math.round(rate) : mock} duration={600} className="text-ink" /> <span className="text-ink-3">msg/s</span>
    </span>
  );
}

function LiveChip() {
  return (
    <span title="Connected to the AEGIS API: data on this screen is live (WS /ws/live)."
      className="num inline-flex items-center gap-1.5 rounded-md border border-ok/30 bg-ok/10 px-2 py-0.5 text-[10px] font-semibold tracking-wider text-ok">
      <span className="inline-block w-1.5 h-1.5 rounded-full bg-ok pulse-dot text-ok" /> LIVE · API
    </span>
  );
}

/** Director mode (F): a scripted, deterministic replay of the §2 demo story. */
const BEATS: { path: string; caption: string; ms: number; act?: () => void }[] = [
  { path: "/intro", caption: "Hook — Cyclone Michaung, Dec 2023", ms: 6500 },
  { path: "/", caption: "Visibility — the network, live", ms: 6000 },
  { path: "/scenario", caption: "What-if — cyclone closes Chennai port for 5 days", ms: 9000, act: () => useAegis.getState().setScenario({ status: "running", type: "cyclone", target: "PORT_CHENNAI", progress: 0 }) },
  { path: "/scenario", caption: "Recommendation — apply Plan A", ms: 4500, act: () => useAegis.getState().applyPlan("A") },
  { path: "/city", caption: "City Twin — trucks reroute around the flood", ms: 7000, act: () => useAegis.getState().setRoadClosed(true) },
  { path: "/trust", caption: "Attack — GPS teleport caught live", ms: 7000, act: () => useAegis.getState().triggerAttack("gps_teleport") },
  { path: "/fidelity", caption: "Proof — measured fidelity", ms: 6000 },
  { path: "/ops", caption: "Scale — self-healing platform", ms: 6000 },
];

function DirectorBar() {
  const { director, setDirector } = useAegis();
  const navigate = useNavigate();
  const [beat, setBeat] = useState(0);
  const timer = useRef<number | undefined>(undefined);
  useEffect(() => {
    if (!director) return;
    setBeat(0);
    const s = useAegis.getState();
    s.resetScenario(); s.applyPlan(null); s.setRoadClosed(false);
    let i = 0;
    const run = () => {
      const b = BEATS[i];
      navigate(b.path);
      setBeat(i);
      window.setTimeout(() => b.act?.(), 600);
      timer.current = window.setTimeout(() => {
        i += 1;
        if (i >= BEATS.length) { setDirector(false); toast("Director mode finished"); return; }
        run();
      }, b.ms);
    };
    run();
    return () => window.clearTimeout(timer.current);
  }, [director, navigate, setDirector]);
  if (!director) return null;
  const b = BEATS[beat];
  return (
    <div className="fixed bottom-5 left-1/2 -translate-x-1/2 z-50 glass px-4 py-2.5 flex items-center gap-4 shadow-2xl">
      <span className="flex items-center gap-2 text-[11px] font-semibold tracking-widest text-ai uppercase"><Clapperboard size={14} /> Director</span>
      <span className="num text-[11px] text-ink-3">{beat + 1}/{BEATS.length}</span>
      <span className="text-[13px] text-ink">{b.caption}</span>
      <div className="w-28 h-1 rounded-full bg-white/10 overflow-hidden">
        <div key={beat} className="h-full bg-ai" style={{ animation: `grow ${b.ms}ms linear forwards` }} />
      </div>
      <button className="text-ink-3 hover:text-ink cursor-pointer" onClick={() => setDirector(false)} aria-label="Stop director mode"><Square size={14} /></button>
      <style>{`@keyframes grow { from { width: 0 } to { width: 100% } }`}</style>
    </div>
  );
}

export function Shell({ children }: { children: ReactNode }) {
  const loc = useLocation();
  const navigate = useNavigate();
  const { setPaletteOpen, setCopilotOpen, copilotOpen, setDirector, director } = useAegis();
  const current = SCREENS.find((s) => s.path === loc.pathname) ?? SCREENS[0];
  const isIntro = loc.pathname === "/intro";
  const live = useLive((st) => st.status !== "offline" || !!st.network);
  const liveScreen = live && (loc.pathname === "/" || loc.pathname === "/scenario");
  useEffect(() => { useLive.getState().connect(); }, []);

  useEffect(() => {
    const onKey = (e: KeyboardEvent) => {
      const tag = (e.target as HTMLElement)?.tagName;
      if (tag === "INPUT" || tag === "TEXTAREA") return;
      const mod = e.metaKey || e.ctrlKey;
      if (mod && e.key.toLowerCase() === "k") { e.preventDefault(); setPaletteOpen(true); return; }
      if (mod && e.key.toLowerCase() === "j") { e.preventDefault(); setCopilotOpen(!useAegis.getState().copilotOpen); return; }
      if (mod || e.altKey) return;
      const scr = SCREENS.find((s) => s.shortcut === e.key);
      if (scr) { navigate(scr.path); return; }
      const k = e.key.toLowerCase();
      if (k === "d") {
        navigate("/scenario");
        useAegis.getState().setScenario({ status: "placed", type: "cyclone", target: "PORT_CHENNAI", progress: 0 });
        toast("Demo disruption placed", { description: "Cyclone over Chennai · 5 days · press Run" });
      }
      if (k === "c") navigate("/trust");
      if (k === "f") setDirector(!useAegis.getState().director);
      if (k === "escape") { setCopilotOpen(false); useAegis.getState().selectNode(null); }
    };
    window.addEventListener("keydown", onKey);
    return () => window.removeEventListener("keydown", onKey);
  }, [navigate, setPaletteOpen, setCopilotOpen, setDirector]);

  return (
    <div className="h-full w-full flex bg-bg">
      {!isIntro && (
        <nav className="w-[64px] shrink-0 h-full flex flex-col items-center py-3 border-r border-line bg-[#080d18] z-30" aria-label="Screens">
          <NavLink to="/intro" className="mb-4 mt-1" title="Replay intro (8)"><Logo /></NavLink>
          <div className="flex flex-col gap-1">
            {SCREENS.filter((s) => s.key !== "intro").map((s) => (
              <NavLink
                key={s.key}
                to={s.path}
                end
                title={`${s.label} (${s.shortcut})`}
                className={({ isActive }) => clsx(
                  "group relative w-11 h-11 grid place-items-center rounded-xl transition-all duration-200",
                  isActive ? "bg-ok/12 text-ok" : "text-ink-3 hover:text-ink hover:bg-white/5",
                )}
              >
                {({ isActive }) => (
                  <>
                    {isActive && <span className="absolute -left-[10px] top-2.5 bottom-2.5 w-[3px] rounded-r bg-ok" />}
                    <s.icon size={19} strokeWidth={1.7} />
                    <span className="num absolute bottom-0.5 right-1 text-[8.5px] opacity-50">{s.shortcut}</span>
                  </>
                )}
              </NavLink>
            ))}
          </div>
          <div className="mt-auto flex flex-col gap-1.5 items-center">
            <button
              onClick={() => setDirector(!director)}
              title="Director mode (F)"
              className={clsx("w-11 h-11 grid place-items-center rounded-xl cursor-pointer transition", director ? "text-ai bg-ai/12" : "text-ink-3 hover:text-ink hover:bg-white/5")}
            ><Clapperboard size={18} strokeWidth={1.7} /></button>
            <button
              onClick={() => setCopilotOpen(!copilotOpen)}
              title="Copilot (⌘J)"
              className={clsx("w-11 h-11 grid place-items-center rounded-xl cursor-pointer transition", copilotOpen ? "text-ai bg-ai/15" : "text-ai/80 hover:text-ai hover:bg-ai/10")}
            ><Sparkles size={19} strokeWidth={1.7} /></button>
          </div>
        </nav>
      )}
      <div className="relative flex-1 min-w-0 h-full">
        {!isIntro && (
          <header className="absolute top-0 inset-x-0 z-20 h-12 flex items-center gap-4 px-4 bg-gradient-to-b from-bg/95 via-bg/70 to-transparent pointer-events-none">
            <div className="flex items-baseline gap-3 pointer-events-auto">
              <span className="text-[13px] font-semibold tracking-wide text-ink">AEGIS <span className="text-ok">Twin</span></span>
              <span className="text-ink-3">/</span>
              <span className="text-[13px] text-ink">{current.label}</span>
              <span className="hidden xl:inline text-[12px] text-ink-3">{current.sub}</span>
            </div>
            <div className="ml-auto flex items-center gap-4 pointer-events-auto">
              {liveScreen ? <LiveChip /> : <MockChip />}
              <IngestRate />
              <Clock />
              <button onClick={() => setPaletteOpen(true)} className="flex items-center gap-2 h-7 pl-2 pr-1.5 rounded-lg border border-line text-[12px] text-ink-3 hover:text-ink hover:border-line-strong cursor-pointer transition">
                <Command size={13} /> Search <Kbd>⌘K</Kbd>
              </button>
            </div>
          </header>
        )}
        <main className="absolute inset-0">{children}</main>
      </div>
      <CommandPalette />
      <CopilotDrawer />
      <DirectorBar />
    </div>
  );
}
