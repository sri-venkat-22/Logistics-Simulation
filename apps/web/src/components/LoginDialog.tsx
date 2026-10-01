import { useEffect, useState } from "react";
import { AnimatePresence, motion } from "motion/react";
import { toast } from "sonner";
import { KeyRound, LogOut, ShieldCheck, User, X } from "lucide-react";
import { getAuthConfig } from "../lib/api";
import { useAuth, type Role } from "../lib/auth";
import { useLive } from "../lib/live";
import { Button } from "./ui";
import { Logo } from "./Shell";

const ROLE_HINT: Record<Role, string> = {
  viewer: "read the network, scenarios and trust feeds",
  planner: "run what-ifs, optimise and apply plans",
  security: "chaos console, key rotation, audit log",
  admin: "everything, including user management",
};

/** OAuth2 password sign-in (POST /api/v1/auth/token). In production the live app is gated behind it. */
export function LoginDialog() {
  const { session, loginOpen, setLoginOpen, login } = useAuth();
  const apiUp = useLive((s) => s.status !== "offline" || !!s.network);
  const [cfg, setCfg] = useState<{ env: string; demo_users: { user: string; role: string; password: string }[] } | null>(null);
  const [user, setUser] = useState("");
  const [pw, setPw] = useState("");
  const [busy, setBusy] = useState(false);
  const [err, setErr] = useState<string | null>(null);
  useEffect(() => { if (apiUp) getAuthConfig().then(setCfg).catch(() => undefined); }, [apiUp]);
  const gate = cfg?.env === "prod" && !session;
  const open = (loginOpen || gate) && apiUp;

  const submit = async (u = user, p = pw) => {
    setBusy(true); setErr(null);
    try {
      const s = await login(u, p);
      toast.success(`Signed in as ${s.user}`, { description: `Role: ${s.role} — ${ROLE_HINT[s.role]}` });
      setPw("");
      useLive.getState().reconnect();
    } catch (e) { setErr((e as Error).message); } finally { setBusy(false); }
  };

  return (
    <AnimatePresence>
      {open && (
        <motion.div className="fixed inset-0 z-[60] grid place-items-center bg-bg/80 backdrop-blur-sm" initial={{ opacity: 0 }} animate={{ opacity: 1 }} exit={{ opacity: 0 }}>
          <motion.form onSubmit={(e) => { e.preventDefault(); void submit(); }} initial={{ y: 12, opacity: 0 }} animate={{ y: 0, opacity: 1 }}
            className="glass w-[380px] p-6 flex flex-col gap-4" aria-label="Sign in">
            <div className="flex items-center gap-3">
              <Logo size={30} />
              <div className="flex-1">
                <div className="text-[15px] font-semibold text-ink">Sign in to AEGIS <span className="text-ok">Twin</span></div>
                <div className="text-[11.5px] text-ink-3">JWT session · role-based access</div>
              </div>
              {!gate && <button type="button" onClick={() => setLoginOpen(false)} className="text-ink-3 hover:text-ink cursor-pointer" aria-label="Close"><X size={16} /></button>}
            </div>
            <label className="flex flex-col gap-1 text-[11.5px] text-ink-3">Username
              <span className="flex items-center gap-2 rounded-lg border border-line bg-white/4 px-2.5 h-9 focus-within:border-ok/60">
                <User size={14} /><input autoFocus autoComplete="username" value={user} onChange={(e) => setUser(e.target.value)} className="flex-1 bg-transparent outline-none text-[13px] text-ink" />
              </span>
            </label>
            <label className="flex flex-col gap-1 text-[11.5px] text-ink-3">Password
              <span className="flex items-center gap-2 rounded-lg border border-line bg-white/4 px-2.5 h-9 focus-within:border-ok/60">
                <KeyRound size={14} /><input type="password" autoComplete="current-password" value={pw} onChange={(e) => setPw(e.target.value)} className="flex-1 bg-transparent outline-none text-[13px] text-ink" />
              </span>
            </label>
            {err && <div role="alert" className="text-[12px] text-bad">{err}</div>}
            <Button variant="primary" type="submit" disabled={busy || !user || !pw} className="h-9">{busy ? "Signing in…" : "Sign in"}</Button>
            {cfg && cfg.demo_users.length > 0 && (
              <div className="border-t border-line pt-3">
                <div className="eyebrow mb-2">Development users (hidden in production)</div>
                <div className="grid grid-cols-2 gap-1.5">
                  {cfg.demo_users.map((d) => (
                    <button type="button" key={d.user} onClick={() => { setUser(d.user); setPw(d.password); void submit(d.user, d.password); }}
                      className="text-left rounded-lg border border-line px-2 py-1.5 hover:border-ok/50 cursor-pointer">
                      <div className="text-[12px] text-ink">{d.user}</div>
                      <div className="text-[10.5px] text-ink-3">{ROLE_HINT[d.role as Role]}</div>
                    </button>
                  ))}
                </div>
              </div>
            )}
          </motion.form>
        </motion.div>
      )}
    </AnimatePresence>
  );
}

/** Header chip: who is signed in (or a Sign in button). */
export function UserChip() {
  const { session, setLoginOpen, logout } = useAuth();
  const apiUp = useLive((s) => s.status !== "offline" || !!s.network);
  if (!apiUp) return null;
  if (!session) {
    return <button onClick={() => setLoginOpen(true)} className="flex items-center gap-1.5 h-7 px-2 rounded-lg border border-line text-[12px] text-ink-2 hover:text-ink hover:border-ok/50 cursor-pointer transition"><User size={13} /> Sign in</button>;
  }
  return (
    <span className="flex items-center gap-2 text-[12px]">
      <span className="flex items-center gap-1.5 h-7 px-2 rounded-lg border border-ok/30 bg-ok/8 text-ink" title={ROLE_HINT[session.role]}>
        <ShieldCheck size={13} className="text-ok" /> {session.user} <span className="num text-[10px] uppercase tracking-wider text-ok">{session.role}</span>
      </span>
      <button onClick={() => { void logout(); toast("Signed out"); }} className="text-ink-3 hover:text-ink cursor-pointer" title="Sign out" aria-label="Sign out"><LogOut size={14} /></button>
    </span>
  );
}
