# How to run AEGIS Twin — step by step

This walks you from a fresh clone to every runnable part of the project:

1. the clickable **prototype** (web UI);
2. the **macro twin** (nation-wide SimPy simulation);
3. the **micro twin** (SUMO traffic simulation of Hyderabad);
4. the **Phase 3 engines**: Monte Carlo, engine verification, the coupled twin and the Reality Emulator;
5. the **backend platform and live UI** (Phases 4–5): API, ingest pipeline, WebSocket, live Control Tower and Scenario Lab;
6. the **tests**;
7. **regenerating** data, diagrams and docs.

Sections 1 and 2 are independent: you can run the web prototype without Python, and the simulations without Node.

---

## 0. Prerequisites

| Tool | Version | Needed for | Check |
|---|---|---|---|
| git | any | cloning | `git --version` |
| Node.js | 20 or newer | web prototype, doc tools | `node --version` |
| Python | 3.11 or newer | simulations, tests | `python3 --version` |
| Google Chrome | any | *only* for regenerating diagrams, PDFs, screenshots | — |
| PostgreSQL + PostGIS | 16+ | the API's database (Phase 4) | `psql --version` |
| Redis | 7+ | the API's streams and cache (Phase 4) | `redis-cli ping` |

On macOS: `brew install postgresql@17 postgis redis && brew services start postgresql@17 && brew services start redis`. With Docker you can use `docker compose up --build` instead (TimescaleDB + Redis + API).

Disk: about 1 GB with all dependencies. Internet is needed for installs and for the map tiles in the prototype.

On **Windows**, use PowerShell and replace `.venv/bin/python` with `.venv\Scripts\python` throughout.

## 1. Get the code

```bash
git clone https://github.com/sri-venkat-22/Logistics-Simulation.git
```

```bash
cd Logistics-Simulation
```

---

## 2. The clickable prototype (web UI)

```bash
cd apps/web && npm install
```

```bash
npm run dev
```

Open **http://localhost:5173**. You should see the *Control Tower*: a dark map of India with animated lanes, KPI cards and an alert feed. Without the API running, every screen carries a **PROTOTYPE · MOCK DATA** badge and runs on seeded mock JSON (`apps/web/src/mock/`). Start the API (section 7) and the Control Tower and Scenario Lab switch to live data and show **LIVE · API**.

**Keyboard**

| Key | Action |
|---|---|
| `1`–`7` | Control Tower, Scenario Lab, City Twin, Trust Center, Fidelity Lab, Network Graph, Ops |
| `8` | Replay the intro (globe → India) |
| `D` | Place the demo disruption (cyclone over Chennai) in the Scenario Lab |
| `C` | Open the Trust Center (chaos console) |
| `F` | Director mode: scripted tour of the whole demo |
| `⌘K` / `Ctrl+K` | Command palette (jump to a screen or node) |
| `⌘J` / `Ctrl+J` | Copilot drawer |

**A 2-minute tour**

1. **Control Tower (`1`)**
   - Click the *HYD-Shamshabad DC* dot to see its time-to-survive (TTS) vs time-to-recover (TTR), inventory and shipments.
   - Drag the timeline at the bottom to **+72 h** to see the predicted state.
2. **Scenario Lab (`2`)**
   - Press `D`, then **Run 500 simulations**.
   - You get split Baseline/Mitigated maps, a fan chart, a Pareto front and ranked plans. Click **Apply Plan A**.
3. **City Twin (`3`)**: click **Flood ORR corridor**; trucks reroute via Uppal / LB Nagar. Try **2k stress** and watch the FPS meter.
4. **Trust Center (`4`)**: click **GPS teleport**, then **30% blackout**.
5. **Network Graph (`6`)**: click **Fail Shenzhen Electronics** to watch the cascade.
6. **Ops (`7`)**: click the skull icon on a pod to watch it self-heal.
7. Press `⌘J` and ask the Copilot the cyclone question.

Production build (static files in `apps/web/dist/`):

```bash
npm run build && npm run preview
```

---

## 3. Python environment (simulations)

From the repository root:

```bash
python3 -m venv .venv
```

```bash
.venv/bin/pip install -r requirements.txt
```

This installs SimPy, NetworkX, Pydantic, pandas, pyosmium and **Eclipse SUMO 1.27.1** (with `libsumo`) as Python wheels. No separate SUMO install is needed. Verify:

```bash
.venv/bin/python -c "import simpy, libsumo, sumolib; print('ok')"
```

---

## 4. The macro twin (SimPy, nation-wide)

The macro twin models the national network: suppliers → ports → distribution centres → 12 city demand zones, for 3 product families (vaccines, FMCG, electronics). It runs in well under a second.

### 4.1 Baseline run (Phase 2 exit command)

```bash
.venv/bin/python -m sim.macro.run --days 30
```

It prints a KPI report:

| Line | Meaning |
|---|---|
| Fill rate | share of demanded units shipped immediately from stock |
| OTIF | share of orders delivered **o**n **t**ime and **i**n **f**ull |
| Backorders at end | units still owed to customers when the run ends |
| Inventory (days of cover) | average stock ÷ average daily demand |
| Cost | transport + holding + stock-out penalty, in ₹ lakh |
| CO₂ | tonnes, from lane distance × weight × mode emission factor |
| SKU table | the same metrics per product family |
| Stock-out hours | which DC ran out of which SKU, for how long |

By default the run starts on **15 Oct 2026**, so it includes the Diwali demand peak (8 Nov, +60% for FMCG and electronics).

### 4.2 Run a disruption

Seven ready-made scenarios live in `sim/scenarios/templates/`:

| Name | What happens |
|---|---|
| `cyclone` | Cyclone over Chennai: nodes inside the track close, lanes slow down |
| `port_closure` | Chennai port closed for 5 days |
| `road_flood` | ORR flooded near Shamshabad |
| `demand_spike` | Hyderabad FMCG + electronics demand × 1.8 for a week |
| `supplier_failure` | Patancheru pharma plant down for 3 days |
| `strike` | Nagpur hub at 30% throughput for 2 days |
| `data_blackout` | 30% of telemetry sources silent (no physical effect; for the trust layer) |

Run one and compare it with the baseline. `--compare` runs both with the same random numbers, so the difference is due to the disruption only:

```bash
.venv/bin/python -m sim.macro.run --days 30 --scenario cyclone --compare
```

Combine several disruptions by repeating `--scenario`:

```bash
.venv/bin/python -m sim.macro.run --days 30 --scenario cyclone --scenario demand_spike --compare
```

### 4.3 Useful options

| Option | Default | Example |
|---|---|---|
| `--days` | 30 | `--days 60` |
| `--start` | `2026-10-15T00:00` (IST) | `--start 2026-12-01T00:00` |
| `--seed` | 42 | `--seed 7` (different random draws) |
| `--scenario` | none | a template name or a path to your own JSON |
| `--compare` | off | also run and print the baseline |
| `--json FILE` | — | save all KPIs as JSON |
| `--events FILE` | — | save the event log (replenishments, receipts, disruptions) as CSV |

### 4.4 Write your own scenario

Copy a template, edit it, and pass the path:

```bash
cp sim/scenarios/templates/supplier_failure.json my_outage.json
```

Change `"duration_h": 72` to `168` (a 7-day outage), then:

```bash
.venv/bin/python -m sim.macro.run --days 30 --scenario my_outage.json --compare
```

You should see vaccine fill drop to about 86% and stock-outs at the cold-chain DCs.

The format:

```json
{
  "type": "port_closure | cyclone | road_flood | demand_spike | supplier_failure | strike | data_blackout",
  "target": "PORT_CHENNAI",
  "polygon": null,
  "start": "+6h",
  "duration_h": 120,
  "severity": 1.0,
  "params": {}
}
```

- **`target`** is a node ID from `data/nodes.json` (e.g. `PORT_CHENNAI`, `DC_NAGPUR`, `Z_HYD`), a lane ID from `data/lanes.json`, or `*`.
- **`start`** is `now`, `+90m`, `+6h`, `+2d`, or an ISO timestamp.
- **`severity`** scales the effect from 0 (none) to 1 (full).
- **`params`** differs per type. See `sim/scenarios/dsl.py` or the JSON Schema in `sim/scenarios/scenario.schema.json`.

Invalid files are rejected with a clear error. To check them:

```bash
.venv/bin/python -m sim.scenarios.validate
```

---

## 5. The micro twin (SUMO, Hyderabad streets)

The micro twin is a street-level traffic simulation of Hyderabad's western and southern logistics belt (10,670 road segments from OpenStreetMap). The network is **already built and committed** in `sim/micro/hyderabad/`, so there is nothing to download.

### 5.1 Move trucks across Hyderabad (Phase 2 exit command)

```bash
.venv/bin/python -m sim.micro.run
```

It spawns 40 freight trucks between the DCs, plants and city hubs on top of background car traffic, then prints every 5 simulated minutes where six of them are. Each row shows latitude/longitude, speed and street name (Outer Ring Road, Medak Road, …), plus arrivals as they happen. It ends with the speed-up versus real time (typically 100–130×).

Options:

| Option | Default | Example |
|---|---|---|
| `--minutes` | 30 | simulated minutes |
| `--trucks` | 40 | freight trucks to spawn |
| `--every` | 300 | print a position table every N simulated seconds |
| `--flood` | off | close the flooded ORR segment at t = 10 min and reroute trucks |
| `--fcd FILE` | — | write truck positions (JSON lines, every 10 s) |
| `--gui` | off | open **sumo-gui** to watch visually (see the note below) |

```bash
.venv/bin/python -m sim.micro.run --minutes 60 --trucks 120 --flood
```

`--gui` uses the sumo-gui bundled with the wheel. On macOS it needs **XQuartz** (https://www.xquartz.org); on Linux it needs a desktop session. The headless mode above needs neither.

### 5.2 The real-time benchmark (500 trucks + 3,000 cars)

```bash
.venv/bin/python -m sim.micro.bench
```

It prints the peak number of simultaneous vehicles and the real-time factor. A reference run on Apple silicon measured **71.6×** real time overall and **44.4×** in the busiest 5 minutes. That result is recorded in `sim/micro/hyderabad/benchmark.json`; add `--out FILE` to save your own.

---

## 6. Engines, coupling and the Reality Emulator (Phase 3)

Measured results for everything below are in `docs/sim/PHASE3.md`.

### 6.1 Engine verification

Checks the macro engine against known answers: an analytic (s,S) fill rate, an EOQ cost curve, and a cross-check against SupplyNetPy. It takes about 20 s.

```bash
.venv/bin/python -m sim.macro.verify
```

You should see three `PASS` lines. Add `--quick` for a 5-second version.

### 6.2 Which nodes can the network not survive? (TTS / TTR)

```bash
.venv/bin/python -m sim.macro.resilience
```

Each node is taken down in turn. The table shows how long the network survives (TTS), how long the node needs to recover (TTR), whether it is exposed (TTS < TTR), and a Risk Exposure Index. The Ahmedabad FMCG supplier comes out on top.

### 6.3 Monte Carlo from Python

```bash
.venv/bin/python -c "from sim.macro.montecarlo import RunSpec, monte_carlo; from sim.scenarios.dsl import Scenario; r = monte_carlo(RunSpec(days=30, scenarios=[Scenario.load('sim/scenarios/templates/cyclone.json')]), n=200); print(r['wall_s'], 's', r['kpis']['fill_rate'])"
```

It runs 200 replications on all cores (about 3 s on 8 cores) and prints the fill-rate P10/P50/P90. The same seeds always give the same bands.

### 6.4 The coupled twin (SimPy + SUMO under one clock)

```bash
.venv/bin/python -m sim.coupling.run --hours 30 --flood
```

Every line marked `→ SUMO` is a macro shipment turning into trucks on Hyderabad roads. Every `← macro` line is those trucks arriving and the macro twin booking the receipt, with the clock lag, which stays under the 60 s sync step. `--flood` closes the ORR edges from the road_flood template in SUMO and shows the lane-multiplier updates. Add `--realtime 60` to run at demo pace (1 sim-minute per wall-second), or `--background` to load the background car traffic.

### 6.5 SUMO corridor calibration

```bash
.venv/bin/python -m sim.micro.calibrate
```

This takes about 2.5 min: 84 trucks drive the five in-city corridors. It writes `sim/micro/hyderabad/calibration.json`, which the macro twin then uses for its Hyderabad lanes. The file is committed, so run this only after changing the SUMO network.

### 6.6 The Reality Emulator

Stream one simulated hour of telemetry, with 20 labelled attacks, to a file:

```bash
.venv/bin/python -m sim.reality.run --hours 1 --warmup-h 12 --attacks 20 --out telemetry.jsonl.gz --truth truth.json
```

To send it to the ingest API instead (Phase 4), use `--http http://localhost:8000`. Batches are HMAC-signed.

Twin and reality side by side (the Phase 3 exit demo):

```bash
.venv/bin/python -m sim.live --hours 48 --every 6
```

Regenerate the red-team benchmark (about 14 s). The SHA-256 hashes must match `data/benchmark/manifest.json`:

```bash
.venv/bin/python -m sim.reality.bench
```

---

## 7. Backend platform and the live UI (Phases 4–5)

Measured results: `docs/platform/PHASE4_5.md`. You need Postgres and Redis running (see Prerequisites).

**7.1 Create the database and run the migrations**

```bash
createdb aegis && createdb aegis_test
```

```bash
cd services/api && ../../.venv/bin/alembic upgrade head && ../../.venv/bin/alembic -x db_url=postgresql+psycopg://localhost/aegis_test upgrade head && cd ../..
```

**7.2 Start the API** (terminal 1). OpenAPI docs are at http://localhost:8000/docs.

```bash
AEGIS_TWIN_WARMUP_H=58 .venv/bin/uvicorn services.api.app.main:app --port 8000
```

**7.3 Stream the Reality Emulator into it** (terminal 2). This runs 24 simulated hours at 1 sim-minute per second, after a fast 58-hour warm-up that matches the twin's.

```bash
.venv/bin/python -m sim.reality.run --hours 24 --warmup-h 58 --realtime 60 --http http://localhost:8000 --chaos-redis redis://localhost:6379/3 --gps-period 2
```

**7.4 Watch the live diffs** (the Phase 4 exit check):

```bash
npx wscat -c "ws://localhost:8000/ws/live?format=json"
```

**7.5 Open the UI** (terminal 3). With the API up, the Control Tower and Scenario Lab show **LIVE · API**:

```bash
cd apps/web && npm run dev
```

- In the Control Tower, `w` / `i` / `h` fly the camera to World, India or Hyderabad.
- In the Scenario Lab, pick a template, press **Run**, then **Optimise** and **Apply** a plan.

**7.6 Inject an attack** (admin token) and watch it get quarantined in the alert feed:

```bash
curl -X POST localhost:8000/api/v1/chaos/inject -H "Authorization: Bearer dev-admin" -H "Content-Type: application/json" -d '{"type":"gps_teleport"}'
```

**7.7 Load test** (it starts its own isolated API on port 8100):

```bash
.venv/bin/python -m services.api.loadtest
```

---

## 8. Tests

```bash
.venv/bin/python -m pytest
```

That runs 81 tests covering the network data, demand model, scenario DSL, macro engine, Monte Carlo and engine verification, micro twin, coupling, Reality Emulator and the API (about 40 s). The API tests need Redis and the `aegis_test` database, and are skipped otherwise. Three slow tests are skipped by default: the live SUMO benchmark, NFR-3 (200 Monte Carlo reps in under 20 s) and byte-identical benchmark regeneration. To run them:

```bash
.venv/bin/python -m pytest -m slow
```

---

## 9. Regenerating data (only if you change inputs)

Everything generated is committed, so you only need these if you edit the sources.

**Network** (nodes, lanes, SKUs, sourcing). Road distances come from the public OSRM server and are cached in `data/osrm_lanes.json`; add `--refresh` to re-query them.

```bash
.venv/bin/python data/generate_network.py
```

**Demand parameters from DataCo.** First download `DataCoSupplyChainDataset.csv` (96 MB) from https://data.mendeley.com/datasets/8gx2fvg2k6/5 into `data/dataco/`, then:

```bash
.venv/bin/python data/demand/fit_dataco.py
```

**SUMO network.** `--download` fetches the Telangana OpenStreetMap extract (104 MB, checksum-verified). Omit it if `sim/micro/hyderabad/osm/telangana.osm.pbf` is already present.

```bash
.venv/bin/python -m sim.micro.build_hyderabad --download
```

**Prototype mock data**, after changing the network:

```bash
.venv/bin/python scripts/gen_mock.py
```

**Docs: diagrams, PDFs, screenshots.** Install the tools first (they use your installed Google Chrome):

```bash
cd tools && npm install
```

Render every Mermaid diagram under `docs/` to PNG and SVG:

```bash
node diagrams.mjs
```

Build the SRS and roadmap PDFs and the research slide:

```bash
node build-pdf.mjs
```

For screenshots, start the prototype (`npm run dev` in `apps/web`), then from `tools/`:

```bash
node screenshots.mjs http://localhost:5173/ ../docs/wireframes
```

---

## 10. Deploy the prototype (Vercel)

Log in to Vercel once:

```bash
cd apps/web && npx vercel login
```

Then deploy:

```bash
npx vercel deploy --prod
```

The app uses hash routing, so it works on any static host. You can also upload `apps/web/dist/` after `npm run build`.

---

## 11. Troubleshooting

| Symptom | Fix |
|---|---|
| Map area is black / grey in the prototype | The basemap tiles come from CARTO and OpenFreeMap, so check your internet. The animated layers still work offline. |
| `Port 5173 is in use` | Stop the other dev server, or run `npm run dev -- --port 5174`. |
| `ModuleNotFoundError: simpy` / `libsumo` | You're not using the venv. Run commands with `.venv/bin/python …` (Windows: `.venv\Scripts\python …`). |
| `micro-twin not built` | Run `.venv/bin/python -m sim.micro.build_hyderabad`. |
| `Warning: Environment variable SUMO_HOME is not set` | Harmless. The code sets it for the bundled SUMO. |
| sumo-gui won't open on macOS | Install XQuartz, log out and in again, or use the headless run. |
| UI still shows PROTOTYPE · MOCK DATA with the API up | Check http://localhost:8000/healthz. For a non-default URL set `VITE_API_URL` in `apps/web/.env.local` (see `.env.example`) and restart `npm run dev`. |
| `readyz` reports `db: false` | Create the database and run the migrations (section 7.1). The API still runs without persistence. |
| The map is live but no trucks move | Start the Reality Emulator (section 7.3); vehicles only exist while telemetry arrives. |
| Python older than 3.11 | Install 3.11+ (e.g. from python.org or with `brew install python@3.12`) and recreate `.venv`. |

## 12. Where things are

| Path | What |
|---|---|
| `apps/web/` | React + deck.gl + MapLibre prototype |
| `sim/macro/` | SimPy macro twin: `network.py`, `demand.py`, `entities.py`, `policies.py`, `engine.py`, `kpis.py`, `montecarlo.py`, `resilience.py`, `verify.py`, `run.py` |
| `sim/micro/` | SUMO micro twin: `build_hyderabad.py`, `runner.py`, `process.py`, `calibrate.py`, `run.py`, `bench.py` |
| `sim/coupling/` | Coupling orchestrator (`orchestrator.py`) and `run.py` |
| `sim/reality/` | Reality Emulator: `emulator.py`, `schemas.py`, `telemetry.py`, `attacks.py`, `run.py`, `bench.py` |
| `data/benchmark/` | Red-team benchmark: labels, truth, manifest (telemetry is regenerated) |
| `sim/scenarios/` | Scenario DSL, 7 templates, JSON Schema |
| `services/api/` | FastAPI platform, Alembic migrations, load test |
| `docker-compose.yml`, `infra/docker/` | TimescaleDB + Redis + API stack |
| `data/` | Network, SKUs, sourcing, demand parameters, festival calendar |
| `docs/srs/` | Software Requirements Specification (PDF + Markdown) |
| `docs/world/WORLD.md` | Phase 2 report with measured results and findings |
| `docs/sim/PHASE3.md` | Phase 3 report: engines, verification, coupling, Reality Emulator, benchmark |
| `docs/platform/PHASE4_5.md` | Phases 4–5 report: API, ingest pipeline, WebSocket, live UI |
| `docs/architecture/`, `docs/dfd/`, `docs/erd/` | Diagrams |
| `docs/roadmap/` | Gantt, phase plan, risk register |
| `PLAN.md` | The full build plan (Levels 1–4) |
