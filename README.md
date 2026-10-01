# AEGIS Twin — Logistics Network Digital Twin (PNT1)

[![CI](https://github.com/sri-venkat-22/Logistics-Simulation/actions/workflows/ci.yml/badge.svg)](https://github.com/sri-venkat-22/Logistics-Simulation/actions/workflows/ci.yml)

**Live (HTTPS, sign-in required): https://thunderously-unwitty-chau.ngrok-free.dev** · title slide [`docs/pitch/title-slide.png`](docs/pitch/title-slide.png)

> The interim public URL is an ngrok tunnel to the production stack (`scripts/live_tunnel.sh`). ngrok shows a one-time "You are about to visit" notice; choose *Visit Site*. Accounts are given on request (`AEGIS_ENV=prod` disables the demo users). The permanent home is a cloud VM + `.tech` domain deployed by CD ([runbook](docs/platform/PHASE8.md)).

**New here? Start with the step-by-step [tutorial](docs/TUTORIAL.md).**

> *See every shipment. Simulate every shock. Survive every attack.*

A two-scale digital twin of an Indian logistics network. SimPy runs the nation-wide macro twin; Eclipse SUMO runs a street-level micro twin of the Hyderabad logistics belt. A 9-layer trust pipeline keeps spoofed or corrupted data out, Monte Carlo what-ifs produce ranked mitigation plans, and a Fidelity Lab scores the twin against reality.

## Level 1 submission (Phase 1 — idea, architecture, planning)

| Deliverable | Where |
|---|---|
| **SRS (PDF)**, IEEE 830-lite: personas, FR-1…FR-25 by PNT1 feature, NFR targets, traceability | [`docs/srs/SRS.pdf`](docs/srs/SRS.pdf) · source [`docs/srs/SRS.md`](docs/srs/SRS.md) |
| **Architecture images**: C4 Context + Container; sequences for ingest, what-if, apply plan | [`docs/architecture/`](docs/architecture/) (`.png` + `.svg` + `.mmd` source) |
| **DFD images**: Level 0 + Level 1 | [`docs/dfd/`](docs/dfd/) |
| **ERD** (schema from PLAN §7) | [`docs/erd/`](docs/erd/) |
| **Clickable prototype**: all 9 screens of §6.5, static mock JSON | [`apps/web`](apps/web) · **URL:** see [Prototype URL](#prototype-url) |
| **Prototype screenshots** (20, captured by an automated walkthrough) | [`docs/wireframes/`](docs/wireframes/) |
| **Tech stack + roadmap**: Gantt Phases 2–11, risk register with mitigations | [`docs/roadmap/ROADMAP.pdf`](docs/roadmap/ROADMAP.pdf) · [`ROADMAP.md`](docs/roadmap/ROADMAP.md) |
| **Research slide**: 8 papers/repos → design choices | [`docs/pitch/research-slide.png`](docs/pitch/research-slide.png) · [`.pdf`](docs/pitch/research-slide.pdf) |
| **ADRs** (5 key decisions) | [`docs/adr/`](docs/adr/) |

### Prototype URL

The Level-1 demo used a temporary Vercel deployment, which has since expired. The live system (Level 3) is at the URL at the top of this page. To host the static prototype yourself, see [Deploying the prototype](#deploying-the-prototype), or run it locally.

> Every number in the prototype is **seeded mock data** (`scripts/gen_mock.py`) and every screen says so with a *PROTOTYPE · MOCK DATA* badge. Geography is real: node coordinates are real sites, and Hyderabad road geometry comes from OpenStreetMap via OSRM.

## Phase 2 — World building (data, demand, SUMO Hyderabad, scenario DSL)

Report with all measured results: [`docs/world/WORLD.md`](docs/world/WORLD.md).

```bash
python3 -m venv .venv && .venv/bin/pip install -r requirements.txt
```

```bash
.venv/bin/python -m sim.macro.run --days 30
```

```bash
.venv/bin/python -m sim.micro.run
```

- **Macro twin** (SimPy): 28 real-coordinate nodes, 49 lanes + 4 transfer-only DC↔DC lanes (Phase 7) (OSRM road distances, sea routes via Malacca), 3 SKU families, demand fitted from DataCo with a Diwali +60% spike. `--scenario cyclone --compare` shows the scenario next to the baseline, using common random numbers.
- **Micro twin** (SUMO 1.27, libsumo): the Hyderabad western/southern belt, 10,670 edges, 12 DC/plant hubs as parkingAreas, background traffic. 500 trucks + 3,000 cars run at **71.6× real time** (`python -m sim.micro.bench`).
- **Scenario DSL** (Pydantic v2): 7 templates in `sim/scenarios/templates/`, validated by `python -m sim.scenarios.validate`.
- Tests: `.venv/bin/python -m pytest` (38 tests).

## Phase 3 — Simulation engines and coupling

Report with all measured results: [`docs/sim/PHASE3.md`](docs/sim/PHASE3.md). Engine verification is SRS §8.

```bash
.venv/bin/python -m sim.macro.verify
```

```bash
.venv/bin/python -m sim.coupling.run --hours 30 --flood
```

```bash
.venv/bin/python -m sim.live --hours 48 --every 6
```

- **Macro engine** (SimPy):
  - Entities: Supplier (production lines as a `Resource`, random failure/recovery), Port (berths as a `Resource`, customs), Warehouse (per-SKU `Container`, (s,S) / (R,Q) / forecast-driven policies), Lane, DemandZone.
  - Fulfilment from the nearest DC with stock.
  - An event log that evidences every KPI, and TTS/TTR per node.
  - The `Twin.run_until` / `snapshot` / `apply` API.
- **Monte Carlo**: `monte_carlo(spec, n, seeds)` gives P10/P50/P90 bands with deterministic seeds. 200 reps × 30 days take **2.8 s** on 8 cores (NFR-3 target < 20 s).
- **Engine verification**: analytic (s,S) fill rate within 0.03%, EOQ minimum exact, and a SupplyNetPy cross-check within 0.01 pp.
- **Micro engine**: SUMO in its own process behind a command queue (lock-step or time-compressed). Corridor travel-time calibration feeds the macro Hyderabad lanes.
- **Coupling**: one clock. Macro shipments entering Hyderabad become SUMO trucks, SUMO arrivals become macro receipts, and SUMO road closures update macro lane multipliers. In a 30-hour run, receipt lag was at most one 60 s sync step.
- **Reality Emulator**: a separately seeded twin with hidden perturbations streams HMAC-signed telemetry (1 Hz GPS, stock counts, ASNs, port status) to a file or the ingest API. An attack injector labels every injection. The red-team benchmark holds **561 labelled attacks** across 10 types (`python -m sim.reality.bench`).
- Tests: see Phase 4–5 below for the current count.

## Phases 4–5 — Backend platform, ingestion and the live UI

Report with all measured results: [`docs/platform/PHASE4_5.md`](docs/platform/PHASE4_5.md). It needs local Postgres (PostGIS) and Redis; Docker users can run `docker compose up --build` instead, which uses TimescaleDB.

```bash
createdb aegis && (cd services/api && ../../.venv/bin/alembic upgrade head)
```

```bash
AEGIS_TWIN_WARMUP_H=58 .venv/bin/uvicorn services.api.app.main:app --port 8000
```

```bash
.venv/bin/python -m sim.reality.run --hours 24 --warmup-h 58 --realtime 60 --http http://localhost:8000 --chaos-redis redis://localhost:6379/3
```

- **Ingest**: `POST /api/v1/ingest/{telemetry|inventory|supplier}` and `WS /api/v1/ingest/stream`, with HMAC publisher signatures, nonce replay protection and strict layer-1 validation, feeding Redis Streams. Measured **27.3k msgs/s** at the gateway and **10.3k msgs/s end to end** on one process (NFR-1 target ≥ 5k).
- **Pipeline**: `telemetry.raw` → trust stage (L2 HMAC, L3 dedupe/replay/stale, L4 physics) → `telemetry.clean` → twin-state stage. That stage updates Redis hashes and NetworkX attributes and batches COPY into Postgres (§7 schema via Alembic, TimescaleDB hypertables + compression when available).
- **WebSocket**: `WS /ws/live` sends a snapshot then 5–10 Hz msgpack diffs (`?format=json` for `wscat`). `WS /ws/scenarios/{id}` streams progress.
- **REST**: network, nodes, shipments, KPIs; scenarios (worker pool, results cached by spec hash, ≈ 5 ms on repeat), optimise, apply plans (planner role); chaos injection (admin only); trust + eval read models; health, readiness and Prometheus metrics. OpenAPI at http://localhost:8000/docs.
- **Live UI** (`cd apps/web && npm run dev`):
  - The Control Tower on the live stream: MapLibre globe, World → India → Hyderabad presets, nodes with pulses, arcs, truck trails, 3-D stock bars, and KPIs / alerts from the API.
  - Trucks are interpolated at 60 fps.
  - Scenario Lab v1: template → run → fan chart + KPI deltas → optimise → apply.
  - With the API down the UI falls back to the Level-1 prototype.
- Tests: `.venv/bin/python -m pytest` (81 tests), plus `-m slow` (3).

## Phase 7 — Intelligence (optimiser, criticality, ML, Copilot, 9-layer trust)

Report with all measured results: [`docs/intelligence/PHASE7.md`](docs/intelligence/PHASE7.md) · trust layer: [`docs/trust/TRUST.md`](docs/trust/TRUST.md).

```bash
.venv/bin/python -m sim.optimize.report && .venv/bin/python -m sim.optimize.criticality
```

```bash
.venv/bin/python -m ml.eta && .venv/bin/python -m ml.forecast && .venv/bin/python -m ml.anomaly
```

- **Optimiser.** Candidates come from k-shortest reroutes (NetworkX), OR-Tools min-cost-flow stock transfers, air expedite and safety-stock buffers. Each one is evaluated with Monte Carlo on common seeds, then ranked by Pareto front plus a weighted score (Scenario Lab sliders). Every plan carries an event-log explanation, and `POST /plans/{id}/apply` pushes it into the live twin and to the fleet, where SUMO trucks visibly take the new routes and transfers (drawn violet). On a 21-day Chennai port closure the top plan cuts the CVaR₉₅ shortfall by **89 %**.
- **Criticality.** Betweenness, a Motter–Lai cascade and the Simchi-Levi Risk Exposure Index rank the single points of failure. The Network Graph animates the cascade in 3-D.
- **ML.**
  - LightGBM ETA quantiles: twin MAE 6.76 h, P10–P90 coverage 78 %. DataCo MAE 1.03 d, late-delivery AUC 0.76.
  - AutoETS + festival calendar demand forecast: WAPE 13.4 %. Fed into (s,S), it lifts fill from 71.9 % to 91.7 %.
  - IsolationForest + MAD ASN anomaly detection: precision 1.0.
- **AI Copilot** (`⌘J`): Claude Opus 5.5 or an offline planner on 8 strict tools, streamed over SSE as tool cards. `propose_apply` only proposes, and a human clicks Apply.
- **Trust pipeline, all 9 layers**: **95.5 %** of 561 red-team attacks detected, 0.19 % false-positive rate.

## Phase 8 — Security, CI/CD and cloud deployment

**Security section: [`docs/security/SECURITY.md`](docs/security/SECURITY.md)** · deployment runbook: [`docs/platform/PHASE8.md`](docs/platform/PHASE8.md).

- **Auth.** OAuth2 password flow → short-lived JWT access tokens (15 min) + rotating refresh tokens (7 d), argon2id password hashes, and RBAC `viewer < planner < security < admin`. Sign in from the top bar.

  | Development user | Password | Can |
  |---|---|---|
  | `viewer` | `aegis-viewer` | Read |
  | `planner` | `aegis-planner` | + run scenarios and apply plans |
  | `security` | `aegis-security` | + chaos console, quarantine, key rotation, audit log |
  | `admin` | `aegis-admin` | Everything |

  These accounts are disabled in production (`AEGIS_ENV=prod`), where users come from `AEGIS_USERS`.
- **Devices.** Per-device HMAC-SHA256 keys, timestamp + nonce replay protection (Redis `SET NX` with a TTL), and `POST /api/v1/devices/{id}/rotate` (pgcrypto-encrypted keys with a grace period).
- **App and transport.** Caddy auto-HTTPS with HSTS + a strict CSP. Strict CORS, slowapi rate limits on Redis, request-size limits, SQLAlchemy-bound parameters only, and secrets from env / GitHub secrets. Prod refuses to start with default secrets.
- **Audit log.** Logins, plan applies, chaos injections, key rotations and Copilot use go to `audit_log` and are shown in the Trust Center.
- **CI** ([`ci.yml`](.github/workflows/ci.yml)): ruff, mypy, pytest with coverage, eslint, tsc, vitest, a Playwright demo smoke test, pip-audit, npm audit, Docker builds, `caddy validate` and Trivy.
- **CD** ([`cd.yml`](.github/workflows/cd.yml)): on `main`, after a green CI run, images are pushed to GHCR. The workflow then runs `docker compose pull && up -d` over SSH on the VM (and optionally a Vercel deploy). The prod stack includes the Reality Emulator as its data source.
- **Live now**: `./scripts/live_tunnel.sh` runs the same production configuration on one machine behind an ngrok HTTPS tunnel: Caddy edge with the repo Caddyfile, `AEGIS_ENV=prod`, generated secrets and accounts in `.env.tunnel`, its own database.
- Tests: `.venv/bin/python -m pytest` (119 tests) · `cd apps/web && npm test && npx playwright test`.

## Run the prototype locally

```bash
cd apps/web && npm install && npm run dev
```

Then open http://localhost:5173. Keyboard: `1–8` switch screens · `D` demo disruption (cyclone over Chennai) · `C` chaos console · `F` Director mode (scripted replay of the demo story) · `⌘K` command palette · `⌘J` Copilot.

### Demo path (about 2 minutes)

1. **Intro** (`8`): the globe flies to India and the network draws in.
2. **Control Tower** (`1`): click *HYD-Shamshabad DC* to see TTS 3.2 d < TTR 5 d. Drag the timeline to +72 h to see the predicted stock-out.
3. **Scenario Lab** (`2`): press `D` → **Run 500 simulations** → split Baseline/Mitigated maps, fan chart and Pareto front → **Apply Plan A**.
4. **City Twin** (`3`): **Flood ORR corridor** → trucks reroute via Uppal / LB Nagar. Try **2k stress** (FPS meter).
5. **Trust Center** (`4`): **GPS teleport**, then **30% blackout** → red ghost vs green Kalman estimate, uncertainty cones, 0 crashes.
6. **Fidelity Lab** (`5`), **Network Graph** (`6`: *Fail Shenzhen Electronics*), **Ops** (`7`: kill a pod).
7. `⌘J`: ask the Copilot the cyclone question; it proposes, and you approve.

## Deploying the prototype

```bash
cd apps/web && npx vercel login
```

```bash
cd apps/web && npx vercel deploy --prod
```

`apps/web/vercel.json` configures the Vite build. The app uses hash routing, so no rewrites are needed.

## Regenerating the artefacts

```bash
python3 data/generate_network.py && python3 scripts/gen_mock.py
```

```bash
cd tools && npm install && node diagrams.mjs && node build-pdf.mjs
```

```bash
cd tools && node screenshots.mjs http://localhost:5173/ ../docs/wireframes
```

The first command regenerates the network data and mock JSON (deterministic; OSRM routes are cached in `data/`). The second renders Mermaid diagrams and builds the PDFs. The third needs the dev server running and drives the prototype to capture the screenshots. The tools use the locally installed Google Chrome (set `CHROME_PATH` if it is elsewhere).

## Repository layout

```
apps/web/            React 19 + deck.gl 9.4 + MapLibre 5 prototype (becomes the real frontend)
sim/macro/           SimPy macro twin: entities, policies, engine (Twin API), KPIs, Monte Carlo, stress test, verification
sim/micro/           SUMO Hyderabad micro-twin: build, libsumo runner, worker process, calibration, `run`, `bench`
sim/coupling/        Coupling orchestrator (one clock, macro <-> SUMO bridge), `python -m sim.coupling.run`
sim/reality/         Reality Emulator, telemetry schemas + HMAC sinks, attack injector, benchmark generator
sim/live.py          Twin and reality side by side (Phase 3 exit demo)
sim/scenarios/       Scenario DSL (Pydantic), 7 templates, JSON Schema
services/api/        FastAPI platform: ingest, trust + twin-state stages, WS fan-out, REST, scenario jobs, Alembic migrations, load test
infra/docker/        API image; docker-compose.yml runs TimescaleDB + Redis + migrations + API (+ emulator profile)
tests/               pytest suite (network, demand, DSL, engine, Monte Carlo + verification, micro, coupling, reality, API)
data/                Network (nodes, lanes, skus, sourcing), demand params + festivals, OSRM cache, red-team benchmark
scripts/gen_mock.py  Seeded mock data for the prototype (incl. a toy Monte Carlo)
docs/                srs · platform · sim · world · architecture · dfd · erd · wireframes · roadmap · pitch · adr
tools/               Doc build tooling: Mermaid render, PDF build, prototype screenshots
PLAN.md              Full build plan (Levels 1–4)
```
