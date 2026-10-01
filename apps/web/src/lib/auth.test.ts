import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { api } from "./api";
import { bearer, ensureRole, hasRole, useAuth } from "./auth";

const tokens = (n: number, role = "planner", ttl = 900) => ({ access_token: `a${n}.b.c`, refresh_token: `r${n}.b.c`, expires_in: ttl, user: "ana", role, token_type: "bearer" });
const ok = (body: unknown) => new Response(JSON.stringify(body), { status: 200, headers: { "Content-Type": "application/json" } });

describe("session", () => {
  beforeEach(() => { sessionStorage.clear(); useAuth.setState({ session: null, loginOpen: false }); });
  afterEach(() => vi.restoreAllMocks());

  it("signs in with the OAuth2 password form and keeps the session in sessionStorage", async () => {
    const f = vi.spyOn(globalThis, "fetch").mockResolvedValue(ok(tokens(1)));
    const s = await useAuth.getState().login("ana", "pw");
    const [url, init] = f.mock.calls[0];
    expect(String(url)).toMatch(/\/api\/v1\/auth\/token$/);
    expect((init!.body as URLSearchParams).get("username")).toBe("ana");
    expect(s.role).toBe("planner");
    expect(JSON.parse(sessionStorage.getItem("aegis.session")!).access).toBe("a1.b.c");
    expect(await bearer()).toBe("a1.b.c");
  });

  it("checks roles by rank and opens the sign-in dialog when a role is missing", async () => {
    vi.spyOn(globalThis, "fetch").mockResolvedValue(ok(tokens(1, "viewer")));
    await useAuth.getState().login("ana", "pw");
    expect(hasRole("viewer")).toBe(true);
    expect(hasRole("planner")).toBe(false);
    expect(ensureRole("security")).toBe(false);
    expect(useAuth.getState().loginOpen).toBe(true);
  });

  it("reports a lockout clearly", async () => {
    vi.spyOn(globalThis, "fetch").mockResolvedValue(new Response(JSON.stringify({ detail: "too many" }), { status: 429 }));
    await expect(useAuth.getState().login("ana", "x")).rejects.toThrow(/Too many attempts/);
  });

  it("refreshes a token that is about to expire before using it", async () => {
    const f = vi.spyOn(globalThis, "fetch").mockResolvedValueOnce(ok(tokens(1, "planner", 10))).mockResolvedValueOnce(ok(tokens(2)));
    await useAuth.getState().login("ana", "pw");
    expect(await bearer()).toBe("a2.b.c");
    expect(String(f.mock.calls[1][0])).toMatch(/\/auth\/refresh$/);
  });

  it("retries a request once after a 401 with a refreshed token", async () => {
    const f = vi.spyOn(globalThis, "fetch")
      .mockResolvedValueOnce(ok(tokens(1)))                                    // login
      .mockResolvedValueOnce(new Response("{}", { status: 401 }))             // access token revoked / expired
      .mockResolvedValueOnce(ok(tokens(2)))                                    // refresh
      .mockResolvedValueOnce(ok({ user: "ana" }));                             // retried request
    await useAuth.getState().login("ana", "pw");
    const r = await api<{ user: string }>("/api/v1/auth/me");
    expect(r.user).toBe("ana");
    expect((f.mock.calls[3][1]!.headers as Record<string, string>).Authorization).toBe("Bearer a2.b.c");
  });

  it("logs out on the server and forgets the session", async () => {
    const f = vi.spyOn(globalThis, "fetch").mockResolvedValue(ok(tokens(1)));
    await useAuth.getState().login("ana", "pw");
    await useAuth.getState().logout();
    expect(useAuth.getState().session).toBeNull();
    expect(sessionStorage.getItem("aegis.session")).toBeNull();
    expect(String(f.mock.calls[1][0])).toMatch(/\/auth\/logout$/);
  });
});
