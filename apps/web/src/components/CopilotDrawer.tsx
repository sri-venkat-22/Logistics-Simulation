import { useEffect, useRef, useState } from "react";
import { AnimatePresence, motion } from "motion/react";
import { useNavigate } from "react-router";
import { toast } from "sonner";
import { Sparkles, X, Wrench, CheckCircle2, ShieldCheck, ArrowUp, Loader2 } from "lucide-react";
import { copilot, scenario, type CopilotStep } from "../lib/data";
import { useAegis } from "../lib/store";
import { Badge, Button, Kbd } from "./ui";
import { useLive } from "../lib/live";
import { LiveCopilotDrawer } from "./LiveCopilot";

function Markdownish({ text }: { text: string }) {
  const parts = text.split(/(\*\*[^*]+\*\*)/g);
  return <>{parts.map((p, i) => (p.startsWith("**") ? <strong key={i} className="text-ink font-semibold">{p.slice(2, -2)}</strong> : <span key={i}>{p}</span>))}</>;
}

function Streaming({ text, onDone }: { text: string; onDone: () => void }) {
  const [n, setN] = useState(0);
  const done = useRef(false);
  useEffect(() => {
    const i = setInterval(() => setN((v) => Math.min(text.length, v + 3)), 16);
    return () => clearInterval(i);
  }, [text]);
  useEffect(() => { if (n >= text.length && !done.current) { done.current = true; onDone(); } }, [n, text, onDone]);
  return <Markdownish text={text.slice(0, n)} />;
}

function MockCopilotDrawer() {
  const { copilotOpen, setCopilotOpen, applyPlan, appliedPlan, setScenario } = useAegis();
  const navigate = useNavigate();
  const [shown, setShown] = useState(0);
  const [started, setStarted] = useState(false);
  const [draft, setDraft] = useState("");
  const endRef = useRef<HTMLDivElement>(null);
  const steps = copilot.script;

  const advance = () => setTimeout(() => setShown((s) => Math.min(steps.length, s + 1)), 450);
  useEffect(() => { endRef.current?.scrollIntoView({ behavior: "smooth", block: "end" }); }, [shown]);
  useEffect(() => {
    // tool calls resolve after a short "running" delay; the scenario result also lands in Scenario Lab
    const cur = steps[shown - 1];
    if (cur?.role === "tool") {
      if (cur.name === "run_scenario") setScenario({ status: "done", type: "cyclone", target: "PORT_CHENNAI", progress: 1 });
      const t = setTimeout(() => setShown((s) => Math.min(steps.length, s + 1)), 1100);
      return () => clearTimeout(t);
    }
    if (cur?.role === "user") advance();
  }, [shown]); // eslint-disable-line react-hooks/exhaustive-deps

  const ask = () => { if (!started) { setStarted(true); setShown(1); setDraft(""); } };
  const plan = scenario.plans.find((p) => p.id === "A")!;

  const render = (s: CopilotStep, i: number) => {
    const last = i === shown - 1;
    if (s.role === "user") return (
      <div key={i} className="self-end max-w-[85%] rounded-2xl rounded-br-md bg-white/8 px-3.5 py-2.5 text-[13px] text-ink">{s.text}</div>
    );
    if (s.role === "assistant") return (
      <div key={i} className="text-[13px] leading-relaxed text-ink-2">
        {last ? <Streaming text={s.text} onDone={advance} /> : <Markdownish text={s.text} />}
      </div>
    );
    if (s.role === "tool") return (
      <div key={i} className="rounded-xl border border-ai/25 bg-ai/6 px-3 py-2.5">
        <div className="flex items-center gap-2 text-[12px]">
          <Wrench size={13} className="text-ai" />
          <span className="num text-ai">{s.name}</span>
          <span className="ml-auto">{last ? <Loader2 size={13} className="animate-spin text-ai" /> : <CheckCircle2 size={13} className="text-good" />}</span>
        </div>
        <div className="num mt-1.5 text-[10.5px] text-ink-3 break-all">{s.args}</div>
        {!last && <div className="mt-1.5 text-[12px] text-ink-2">→ {s.result}</div>}
      </div>
    );
    return (
      <div key={i} className="rounded-xl border border-ai/40 bg-gradient-to-b from-ai/12 to-ai/4 p-3.5">
        <div className="flex items-center gap-2">
          <Badge sev="ai">Proposal · Plan {plan.id}</Badge>
          <span className="ml-auto flex items-center gap-1 text-[10.5px] text-ink-3"><ShieldCheck size={12} /> Human approval required</span>
        </div>
        <div className="mt-2 text-[13px] font-medium text-ink">{plan.name}</div>
        <div className="mt-2 grid grid-cols-3 gap-2 text-center">
          {[["Service", `${plan.service}%`], ["Cost", `₹${plan.cost_lakh} L`], ["CO₂", `${plan.co2_t} t`]].map(([k, v]) => (
            <div key={k} className="rounded-lg bg-black/20 py-1.5"><div className="eyebrow !text-[9px]">{k}</div><div className="num text-[13px] text-ink">{v}</div></div>
          ))}
        </div>
        <div className="mt-3 flex gap-2">
          <Button variant="ai" className="flex-1" disabled={appliedPlan === "A"} onClick={() => {
            applyPlan("A");
            toast.success("Plan A applied", { description: "Routes and transfers pushed to the live twin · audit #A-2291" });
          }}>{appliedPlan === "A" ? "Applied" : "Apply Plan A"}</Button>
          <Button onClick={() => { navigate("/scenario"); setCopilotOpen(false); }}>Open in Scenario Lab</Button>
        </div>
      </div>
    );
  };

  return (
    <AnimatePresence>
      {copilotOpen && (
        <motion.aside
          initial={{ x: 420, opacity: 0 }} animate={{ x: 0, opacity: 1 }} exit={{ x: 420, opacity: 0 }}
          transition={{ type: "tween", duration: 0.3, ease: [0.22, 1, 0.36, 1] }}
          className="fixed right-3 top-3 bottom-3 w-[400px] max-w-[calc(100vw-24px)] z-40 glass !bg-[#0b1220]/92 shadow-2xl flex flex-col"
          aria-label="AI Copilot"
        >
          <div className="flex items-center gap-2 px-4 h-12 border-b border-line">
            <Sparkles size={16} className="text-ai" />
            <span className="text-[13px] font-semibold">Copilot</span>
            <Badge sev="ai" className="ml-1">Evidence-gated</Badge>
            <button onClick={() => setCopilotOpen(false)} className="ml-auto text-ink-3 hover:text-ink cursor-pointer" aria-label="Close copilot"><X size={16} /></button>
          </div>
          <div className="flex-1 overflow-y-auto scroll-thin px-4 py-4 flex flex-col gap-3">
            {!started && (
              <div className="text-[13px] text-ink-2 leading-relaxed">
                <p>I can create scenarios, run simulations, compare plans and explain KPIs. I can <em>propose</em> changes but never apply them — you click Apply.</p>
                <div className="mt-4 eyebrow">Try</div>
                <div className="mt-2 flex flex-col gap-1.5">
                  {["What if a cyclone closes Chennai port for 5 days?", "Which nodes are single points of failure?", "Why did OTIF drop 1.1 pp today?"].map((q, i) => (
                    <button key={q} onClick={() => (i === 0 ? ask() : toast("Scripted prototype", { description: "Only the demo question is wired in Level 1." }))}
                      className="text-left rounded-lg border border-line hover:border-ai/40 hover:bg-ai/5 px-3 py-2 text-[12.5px] text-ink-2 cursor-pointer transition">{q}</button>
                  ))}
                </div>
              </div>
            )}
            {steps.slice(0, shown).map(render)}
            <div ref={endRef} />
          </div>
          <form className="p-3 border-t border-line" onSubmit={(e) => { e.preventDefault(); ask(); }}>
            <div className="flex items-center gap-2 rounded-xl border border-line-strong bg-black/20 pl-3 pr-1.5 h-11">
              <input value={draft} onChange={(e) => setDraft(e.target.value)} placeholder="Ask about the network…" className="flex-1 bg-transparent outline-none text-[13px] placeholder:text-ink-3" />
              <Kbd>⌘J</Kbd>
              <button type="submit" className="w-8 h-8 grid place-items-center rounded-lg bg-ai text-bg cursor-pointer" aria-label="Send"><ArrowUp size={15} /></button>
            </div>
          </form>
        </motion.aside>
      )}
    </AnimatePresence>
  );
}

/** The live Copilot when the API is reachable; the scripted Level-1 walkthrough otherwise. */
export function CopilotDrawer() {
  const apiUp = useLive((s) => s.status !== "offline" || !!s.network);
  return apiUp ? <LiveCopilotDrawer /> : <MockCopilotDrawer />;
}
