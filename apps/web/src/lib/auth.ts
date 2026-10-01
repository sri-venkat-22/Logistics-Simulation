/**
 * Session: OAuth2 password flow against POST /api/v1/auth/token -> short-lived JWT access token + rotating refresh
 * token (services/api/app/security.py). Tokens live in sessionStorage (cleared when the tab closes). The access token
 * is refreshed shortly before it expires and once on a 401; logout revokes both on the server.
 */
import { create } from "zustand";
import { API_URL, DEV_TOKEN } from "./config";

export type Role = "viewer" | "planner" | "security" | "admin";
export const ROLE_RANK: Record<Role, number> = { viewer: 0, planner: 1, security: 2, admin: 3 };

export interface Session { user: string; role: Role; access: string; refresh: string; exp: number }
interface TokenResponse { access_token: string; refresh_token: string; expires_in: number; user: string; role: Role }

const KEY = "aegis.session";
function load(): Session | null {
  try { const raw = sessionStorage.getItem(KEY); return raw ? (JSON.parse(raw) as Session) : null; } catch { return null; }
}
function save(s: Session | null) {
  try { if (s) sessionStorage.setItem(KEY, JSON.stringify(s)); else sessionStorage.removeItem(KEY); } catch { /* private mode */ }
}
const toSession = (t: TokenResponse): Session => ({ user: t.user, role: t.role, access: t.access_token, refresh: t.refresh_token,
  exp: Date.now() + t.expires_in * 1000 });

interface AuthState {
  session: Session | null;
  loginOpen: boolean;
  setLoginOpen: (v: boolean) => void;
  login: (user: string, password: string) => Promise<Session>;
  logout: () => Promise<void>;
  refresh: () => Promise<boolean>;
}

let refreshing: Promise<boolean> | null = null;

export const useAuth = create<AuthState>((set, get) => ({
  session: load(),
  loginOpen: false,
  setLoginOpen: (v) => set({ loginOpen: v }),
  login: async (user, password) => {
    const r = await fetch(`${API_URL}/api/v1/auth/token`, { method: "POST", headers: { "Content-Type": "application/x-www-form-urlencoded" },
      body: new URLSearchParams({ username: user, password }) });
    if (!r.ok) {
      const detail = await r.json().then((j) => j.detail as string).catch(() => r.statusText);
      throw new Error(r.status === 429 ? "Too many attempts — try again in a few minutes." : detail || "Sign-in failed");
    }
    const s = toSession(await r.json());
    save(s); set({ session: s, loginOpen: false });
    return s;
  },
  logout: async () => {
    const s = get().session;
    save(null); set({ session: null });
    if (s) {
      await fetch(`${API_URL}/api/v1/auth/logout`, { method: "POST", headers: { "Content-Type": "application/json", Authorization: `Bearer ${s.access}` },
        body: JSON.stringify({ refresh_token: s.refresh }) }).catch(() => undefined);
    }
  },
  refresh: async () => {
    const s = get().session;
    if (!s) return false;
    refreshing ??= (async () => {
      try {
        const r = await fetch(`${API_URL}/api/v1/auth/refresh`, { method: "POST", headers: { "Content-Type": "application/json" },
          body: JSON.stringify({ refresh_token: s.refresh }) });
        if (!r.ok) { save(null); set({ session: null }); return false; }
        const ns = toSession(await r.json());
        save(ns); set({ session: ns });
        return true;
      } catch { return false; } finally { refreshing = null; }
    })();
    return refreshing;
  },
}));

/** The bearer token to send: the signed-in user's access token (refreshed if about to expire), else the dev token. */
export async function bearer(): Promise<string | null> {
  const st = useAuth.getState();
  if (st.session && st.session.exp - Date.now() < 30_000) await st.refresh();
  return useAuth.getState().session?.access ?? DEV_TOKEN;
}

export function hasRole(min: Role): boolean {
  const s = useAuth.getState().session;
  if (s) return ROLE_RANK[s.role] >= ROLE_RANK[min];
  return DEV_TOKEN !== null && ROLE_RANK.planner >= ROLE_RANK[min];   // dev token = planner
}

/** Ask the user to sign in if they lack `min`; returns true when they already have it. */
export function ensureRole(min: Role): boolean {
  if (hasRole(min)) return true;
  useAuth.getState().setLoginOpen(true);
  return false;
}
