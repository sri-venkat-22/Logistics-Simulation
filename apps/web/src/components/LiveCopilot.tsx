/**
 * The Copilot on the live API (Phase 7.4): streamed answer text, tool calls as cards, and a proposal card whose
 * Apply button the human clicks (the Copilot itself can never change the live twin).
 */
import { useEffect, useRef, useState } from "react";
import { AnimatePresence, motion } from "motion/react";
import { useNavigate } from "react-router";
import { toast } from "sonner";
import { ArrowUp, CheckCircle2, Loader2, ShieldCheck, Sparkles, Square, Wrench, X, XCircle } from "lucide-react";
import { applyPlan, getCopilotStatus } from "../lib/api";
import { ensureRole } from "../lib/auth";
import { streamChat, type ChatTurn, type CopilotEvent } from "../lib/copilot";
import { useAegis } from "../lib/store";
import { Badge, Button, Kbd } from "./ui";

type Item =
  | { kind: "user"; text: string }
  | { kind: "text"; text: string }
  | { kind: "tool"; id: string; name: string; input: unknown; ok?: boolean; summary?: string }
  | { kind: "proposal"; p: Extract<CopilotEvent, { type: "proposal" }> }
  | { kind: "error"; text: string };

function Markdownish({ text }: { text: string }) {
  return (
    <>{text.split("\n").map((line, j) => (
      <p key={j} className={line ? "" : "h-2"}>
        {line.split(/(\*\*[^*]+\*\*)/g).map((p, i) => (p.startsWith("**") ? <strong key={i} className="text-ink font-semibold">{p.slice(2, -2)}</strong> : <span key={i}>{p}</span>))}
      </p>
    ))}</>
  );
}

const SUGGEST = ["What if a cyclone closes Chennai port for 5 days?", "Which nodes are most at risk?",
  "What if the Patancheru plant has a 3 day outage?", "Explain the fill rate", "How is the network doing?"];

export function LiveCopilotDrawer() {
  const { copilotOpen, setCopilotOpen } = useAegis();
  const navigate = useNavigate();
  const [items, setItems] = useState<Item[]>([]);
  const [history, setHistory] = useState<ChatTurn[]>([]);
  const [draft, setDraft] = useState("");
  const [busy, setBusy] = useState(false);
  const [mode, setMode] = useState<{ mode: string; model: string | null } | null>(null);
  const [applied, setApplied] = useState<string | null>(null);
  const abort = useRef<AbortController | null>(null);
  const endRef = useRef<HTMLDivElement>(null);
  useEffect(() => { if (copilotOpen) getCopilotStatus().then(setMode).catch(() => undefined); }, [copilotOpen]);
  useEffect(() => { endRef.current?.scrollIntoView({ behavior: "smooth", block: "end" }); }, [items]);

  const send = async (q: string) => {
    q = q.trim();
    if (!q || busy) return;
    const turns: ChatTurn[] = [...history, { role: "user", content: q }];
    setItems((xs) => [...xs, { kind: "user", text: q }]);
    setDraft(""); setBusy(true);
    let answer = "";
    const ctl = new AbortController();
    abort.current = ctl;
    const onEvent = (e: CopilotEvent) => {
      if (e.type === "text") {
        answer += e.delta;
        setItems((xs) => {
          const last = xs[xs.length - 1];
          return last?.kind === "text" ? [...xs.slice(0, -1), { kind: "text", text: last.text + e.delta }] : [...xs, { kind: "text", text: e.delta }];
        });
      } else if (e.type === "tool_call") {
        setItems((xs) => [...xs, { kind: "tool", id: e.id, name: e.name, input: e.input }]);
      } else if (e.type === "tool_result") {
        setItems((xs) => xs.map((x) => (x.kind === "tool" && x.id === e.id ? { ...x, ok: e.ok, summary: e.summary } : x)));
      } else if (e.type === "proposal") {
        setItems((xs) => [...xs, { kind: "proposal", p: e }]);
      } else if (e.type === "error") {
        setItems((xs) => [...xs, { kind: "error", text: e.message }]);
      }
    };
    try {
      await streamChat(turns, onEvent, ctl.signal);
      setHistory([...turns, { role: "assistant" as const, content: answer || "(tool results shown above)" }].slice(-20));
    } catch (e) {
      if ((e as Error).name !== "AbortError") {
        const status = (e as { status?: number }).status;
        setItems((xs) => [...xs, { kind: "error", text: status === 401 ? "Sign in to use the Copilot." : String((e as Error).message) }]);
        if (status === 401) ensureRole("viewer");
      }
    } finally { setBusy(false); abort.current = null; }
  };

  const apply = (planId: string, name: string) => {
    if (!ensureRole("planner")) return;
    applyPlan(planId).then((r) => { setApplied(planId); toast.success(`Applied: ${name}`, { description: `${r.acks.filter((a) => a.ok).length} actions pushed by ${r.applied_by} · audited` }); })
      .catch((e) => toast.error("Apply failed", { description: String(e) }));
  };

  return (
    <AnimatePresence>
      {copilotOpen && (
        <motion.aside initial={{ x: 420, opacity: 0 }} animate={{ x: 0, opacity: 1 }} exit={{ x: 420, opacity: 0 }}
          transition={{ type: "tween", duration: 0.3, ease: [0.22, 1, 0.36, 1] }}
          className="fixed right-3 top-3 bottom-3 w-[420px] max-w-[calc(100vw-24px)] z-40 glass !bg-[#0b1220]/92 shadow-2xl flex flex-col" aria-label="AI Copilot">
          <div className="flex items-center gap-2 px-4 h-12 border-b border-line">
            <Sparkles size={16} className="text-ai" />
            <span className="text-[13px] font-semibold">Copilot</span>
            <Badge sev="ai" className="ml-1">Evidence-gated</Badge>
            {mode && <span className="num text-[10px] text-ink-3" title={mode.mode === "claude" ? "Claude over the Messages API" : "No Anthropic key configured: deterministic planner driving the same tools"}>
              {mode.mode === "claude" ? mode.model : "offline planner"}</span>}
            <button onClick={() => setCopilotOpen(false)} className="ml-auto text-ink-3 hover:text-ink cursor-pointer" aria-label="Close copilot"><X size={16} /></button>
          </div>
          <div className="flex-1 overflow-y-auto scroll-thin px-4 py-4 flex flex-col gap-3" aria-live="polite">
            {items.length === 0 && (
              <div className="text-[13px] text-ink-2 leading-relaxed">
                <p>I run what-ifs on the twin, rank mitigation plans and explain KPIs — every number comes from a tool result. I can <em>propose</em> a plan; a planner applies it.</p>
                <div className="mt-4 eyebrow">Try</div>
                <div className="mt-2 flex flex-col gap-1.5">
                  {SUGGEST.map((q) => (
                    <button key={q} onClick={() => void send(q)} className="text-left rounded-lg border border-line hover:border-ai/40 hover:bg-ai/5 px-3 py-2 text-[12.5px] text-ink-2 cursor-pointer transition">{q}</button>
                  ))}
                </div>
              </div>
            )}
            {items.map((it, i) => {
              if (it.kind === "user") return <div key={i} className="self-end max-w-[85%] rounded-2xl rounded-br-md bg-white/8 px-3.5 py-2.5 text-[13px] text-ink">{it.text}</div>;
              if (it.kind === "text") return <div key={i} className="text-[13px] leading-relaxed text-ink-2"><Markdownish text={it.text} /></div>;
              if (it.kind === "error") return <div key={i} role="alert" className="text-[12.5px] text-bad">{it.text}</div>;
              if (it.kind === "tool") return (
                <div key={i} className="rounded-xl border border-ai/25 bg-ai/6 px-3 py-2.5">
                  <div className="flex items-center gap-2 text-[12px]">
                    <Wrench size={13} className="text-ai" /><span className="num text-ai">{it.name}</span>
                    <span className="ml-auto">{it.ok === undefined ? <Loader2 size={13} className="animate-spin text-ai" /> : it.ok ? <CheckCircle2 size={13} className="text-good" /> : <XCircle size={13} className="text-bad" />}</span>
                  </div>
                  <div className="num mt-1.5 text-[10.5px] text-ink-3 break-all">{JSON.stringify(it.input)}</div>
                  {it.summary && <div className="mt-1.5 text-[12px] text-ink-2">→ {it.summary}</div>}
                </div>
              );
              const p = it.p;
              return (
                <div key={i} className="rounded-xl border border-ai/40 bg-gradient-to-b from-ai/12 to-ai/4 p-3.5">
                  <div className="flex items-center gap-2"><Badge sev="ai">Proposal</Badge>
                    <span className="ml-auto flex items-center gap-1 text-[10.5px] text-ink-3"><ShieldCheck size={12} /> Human approval required</span></div>
                  <div className="mt-2 text-[13px] font-medium text-ink">{p.name}</div>
                  <div className="mt-2 grid grid-cols-3 gap-2 text-center">
                    {[["Service", `${(p.service * 100).toFixed(2)}%`], ["Cost", `₹${p.cost_lakh.toFixed(1)} L`], ["CVaR95", `₹${(p.cvar95_lakh ?? 0).toFixed(1)} L`]].map(([k, v]) => (
                      <div key={k} className="rounded-lg bg-black/20 py-1.5"><div className="eyebrow !text-[9px]">{k}</div><div className="num text-[13px] text-ink">{v}</div></div>
                    ))}
                  </div>
                  <div className="mt-3 flex gap-2">
                    <Button variant="ai" className="flex-1" disabled={applied === p.plan_id} onClick={() => apply(p.plan_id, p.name)}>{applied === p.plan_id ? "Applied" : `Apply (${p.n_actions} actions)`}</Button>
                    <Button onClick={() => { navigate("/scenario"); setCopilotOpen(false); }}>Scenario Lab</Button>
                  </div>
                </div>
              );
            })}
            <div ref={endRef} />
          </div>
          <form className="p-3 border-t border-line" onSubmit={(e) => { e.preventDefault(); void send(draft); }}>
            <div className="flex items-center gap-2 rounded-xl border border-line-strong bg-black/20 pl-3 pr-1.5 h-11">
              <input value={draft} onChange={(e) => setDraft(e.target.value)} placeholder="Ask about the network…" aria-label="Message the Copilot"
                className="flex-1 bg-transparent outline-none text-[13px] placeholder:text-ink-3" />
              <Kbd>⌘J</Kbd>
              {busy
                ? <button type="button" onClick={() => abort.current?.abort()} className="w-8 h-8 grid place-items-center rounded-lg bg-white/10 text-ink cursor-pointer" aria-label="Stop"><Square size={13} /></button>
                : <button type="submit" className="w-8 h-8 grid place-items-center rounded-lg bg-ai text-bg cursor-pointer" aria-label="Send"><ArrowUp size={15} /></button>}
            </div>
          </form>
        </motion.aside>
      )}
    </AnimatePresence>
  );
}
