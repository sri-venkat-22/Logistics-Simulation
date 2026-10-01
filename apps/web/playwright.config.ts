import { defineConfig } from "@playwright/test";

/**
 * Smoke test of the demo flow against a real API. Starts its own API (port 8010, Redis DB 13, Postgres aegis_test)
 * and a Vite dev server (port 5175). Locally it drives the installed Chrome; CI installs Playwright's Chromium.
 *   AEGIS_E2E_DB_URL / AEGIS_E2E_REDIS_URL override the databases; PYTHON the interpreter.
 */
const API = "http://localhost:8010";
const py = process.env.PYTHON ?? ".venv/bin/python";  // relative to the repo root (the API server cwd)

export default defineConfig({
  testDir: "e2e",
  timeout: 120_000,
  expect: { timeout: 20_000 },
  retries: process.env.CI ? 1 : 0,
  reporter: process.env.CI ? [["list"], ["html", { open: "never" }]] : "list",
  use: { baseURL: "http://localhost:5175", viewport: { width: 1440, height: 900 }, trace: "retain-on-failure",
         ...(process.env.CI ? {} : { channel: "chrome" }) },
  webServer: [
    {
      command: `${py} -m uvicorn services.api.app.main:app --port 8010 --log-level warning`,
      cwd: "../..",
      url: `${API}/readyz`,
      timeout: 120_000,
      reuseExistingServer: false,
      env: { AEGIS_REDIS_URL: process.env.AEGIS_E2E_REDIS_URL ?? "redis://localhost:6379/13",
             AEGIS_DB_URL: process.env.AEGIS_E2E_DB_URL ?? "postgresql+psycopg://localhost/aegis_test",
             AEGIS_TWIN_WARMUP_H: "58", AEGIS_SCENARIO_WORKERS: "2", AEGIS_COPILOT: "offline" },
    },
    {
      command: "npx vite --port 5175 --strictPort",
      url: "http://localhost:5175",
      timeout: 60_000,
      reuseExistingServer: false,
      env: { VITE_API_URL: API },
    },
  ],
});
