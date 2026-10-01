/** API endpoints. VITE_API_URL points the UI at the API (default http://localhost:8000; in production the web and the
 * API share an origin behind Caddy, so set VITE_API_URL="" for same-origin requests). */
const env = import.meta.env.VITE_API_URL as string | undefined;
export const API_URL: string = env ?? "http://localhost:8000";
export const WS_URL: string = API_URL ? API_URL.replace(/^http/, "ws") : `${location.protocol === "https:" ? "wss" : "ws"}://${location.host}`;
/** Dev-only fallback bearer token for planner actions before anyone signs in (never used in production builds). */
export const DEV_TOKEN: string | null = import.meta.env.DEV ? ((import.meta.env.VITE_API_TOKEN as string | undefined) ?? "dev-planner") : null;
