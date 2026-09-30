# AEGIS Twin — Logistics Network Digital Twin (PNT1)

**Build plan for the NRCM hackathon: sequential, mapped to Levels 1–4, and aimed at winning.**

> *"See every shipment. Simulate every shock. Survive every attack."*

`AEGIS Twin` is a working name. An aegis is a shield, which fits the adversarial-resilience requirement. Rename it if you like, but choose a name on Day 0 and use it everywhere.

---

## 0. The winning idea in one paragraph

Build a **two-scale co-simulated digital twin** of an Indian logistics network:

- **Macro twin (SimPy):** a nation-wide discrete-event model of suppliers, ports, warehouses, lanes, inventory and orders. It is fast enough to run hundreds of Monte Carlo "what-if" replications in seconds.
- **Micro twin (Eclipse SUMO):** a street-level traffic simulation of the **Hyderabad logistics belt** (Medchal ↔ ORR ↔ Shamshabad pharma cold-storage ↔ Patancheru). Real trucks move on real OpenStreetMap roads, and they reroute live when a road floods.
- **Coupling:** SimPy dispatches spawn SUMO trucks. SUMO arrivals fire SimPy "goods received" events. SUMO travel-time distributions calibrate SimPy's lead times, so you get SUMO for fidelity and SimPy for speed.
- **Trust layer:** every incoming data point is validated, physics-checked, Kalman-filtered and cross-checked against the twin's own prediction before it can change state. Spoofed GPS, corrupted feeds and blackouts are caught live, and the twin keeps running.
- **Fidelity lab:** the twin's predictions are continuously scored against "reality" (MAPE, interval coverage, attack-detection F1, decision value).
- **Frontend:** one cinematic "mission-control" UI. The camera zooms continuously from a 3D globe → India's network arcs → 3D Hyderabad streets with glowing truck trails.

Why this wins:

1. It addresses every bullet in the problem statement with something visible on screen.
2. It uses SUMO and SimPy for what each does best, which is a real technical insight.
3. The globe-to-street zoom is a strong visual moment.
4. The live attack demo shows adversarial resilience instead of only describing it.
5. The judges' home city (Hyderabad) is the hero of the story.

---

## 1. Requirement → feature → on-screen proof

| PNT1 requirement | What we build | What judges *see* |
|---|---|---|
| Multi-component architecture, high-throughput ingestion, graph modelling | FastAPI ingest gateway → Redis Streams → trust workers → twin-state service. NetworkX multimodal graph. Timescale + PostGIS. | Live ingest-rate counter (msgs/s) and an architecture slide backed by a load-test number |
| Scenario-based optimisation, what-if, automated recommendations | SimPy Monte Carlo + OR-Tools min-cost-flow reallocation + k-shortest multimodal rerouting + Pareto/CVaR ranking + TTS/TTR metrics | **Scenario Lab:** drop a "cyclone" on Chennai, see a fan chart of stockout risk, then pick between 3 ranked plans and click **Apply** |
| End-to-end visibility | Live map of orders, stock and lanes. Past / present / future timeline scrubber. | **Control Tower:** trucks, ships and 3D inventory bars on the map. Slide into the future to see predicted stockouts. |
| Adversarial resilience | 9-layer trust pipeline (§6.4) plus a Chaos Console for injecting attacks | Truck "teleports" to Mumbai → red ghost marker + green Kalman estimate + quarantine log. 30% telemetry blackout → uncertainty cones widen and nothing crashes. |
| Production-ready evaluation | Shadow-mode prediction logging, fidelity metrics, red-team benchmark, counterfactual decision value, DataCo real-data backtest | **Fidelity Lab:** MAPE, P90 coverage, attack F1, "cost saved vs baseline" (real measured numbers) |
| Levels 3–4: security, CI/CD, cloud, Docker/K8s, caching, monitoring | JWT + RBAC, HMAC-signed devices, TLS, GitHub Actions, K8s + HPA/KEDA, Redis cache, Prometheus/Grafana/Alertmanager | Kill a pod during the demo and it self-heals. Grafana dashboard. Public HTTPS URL. |

---

## 2. Demo story (design everything backwards from this)

**"Cyclone over Chennai" — 4 minutes.** Build only what this story needs first, then add depth.

| Time | Beat | Screen |
|---|---|---|
| 0:00 | **Hook.** "In December 2023 Cyclone Michaung flooded Chennai for days. Every pharma distributor in Hyderabad found out *after* their shelves were empty." (Also usable: Suez/Ever Given 2021, Red Sea reroutes 2024.) | Black screen → globe intro |
| 0:30 | **Visibility.** Globe flies to India and the network lights up. Hyderabad trucks stream on real roads. Show live weather (Open-Meteo) and, optionally, real ships near Indian ports (AISStream). | Control Tower |
| 1:00 | **What-if.** A planner types into the Copilot: *"What if a cyclone closes Chennai port for 5 days?"* 500 Monte Carlo runs finish in about 10 s. Fan chart: Hyderabad pharma DC stocks out in **3.2 days (TTS)**, but the port needs **5 days to recover (TTR)**, so the network is exposed. | Scenario Lab |
| 1:45 | **Recommendation.** Three ranked plans on a Pareto chart (cost vs service vs CO₂): A = reroute via Vizag + transfer stock from Bengaluru, B = air-expedite, C = do nothing. Click **Apply A**. Arcs re-route on the map, and in the Hyderabad 3D view trucks physically change roads. | Scenario Lab → City Twin |
| 2:30 | **Attack.** Open the Chaos Console: (1) spoof GPS so a truck jumps 600 km; (2) corrupt a supplier feed to claim 1,000,000 units; (3) black out 30% of telemetry. The trust layer catches each one live. The twin switches to predictive dead-reckoning, uncertainty cones widen, and there are zero crashes. | Trust Center |
| 3:15 | **Proof.** Fidelity Lab shows real numbers you measured, e.g. ETA MAPE, P90 coverage, attack-detection precision/recall, and "stockouts avoided / cost saved across 50 backtested disruptions". | Fidelity Lab |
| 3:45 | **Scale.** Architecture slide, k6 load-test result, Grafana. **Kill a pod live** and watch Kubernetes restore it. | Ops |
| 4:15 | Close with the tagline and the ask. | — |

**Rule:** every number shown on stage must come from your own runs. Judges ask, and honest numbers read as confidence.

---

## 3. Tech stack (final)

### Simulation and intelligence (Python 3.12)

| Concern | Choice | Why |
|---|---|---|
| Macro DES | **SimPy 4.1** (custom engine, ~600–900 LOC) | Full control over live hooks. Borrow patterns from [SupplyNetPy](https://github.com/SupplyChainSimulation/SupplyNetPy) ((s,S)/(R,Q) policies, disruptions) and use it as a validation reference. |
| Live-sync DES | `simpy.rt.RealtimeEnvironment(factor, strict=False)`; optionally [dynamic-des](https://github.com/jaehyeon-kim/dynamic-des) for live parameter switchboard | Twin runs on wall-clock time while live data changes parameters |
| Micro traffic | **Eclipse SUMO 1.27** (`pip install eclipse-sumo libsumo traci sumolib`, macOS arm64 wheels available) | libsumo is in-process and fast, TraCI is for GUI debugging. Hyderabad network from OSM via `osmWebWizard.py` / `netconvert`. |
| Graph | **NetworkX** | Multimodal graph, k-shortest paths, betweenness centrality, cascade simulation |
| Optimisation | **OR-Tools** (`SimpleMinCostFlow`, VRP), **PuLP + HiGHS** for multi-SKU LP | Inventory reallocation, rerouting, last-mile |
| ML | scikit-learn (IsolationForest), **LightGBM** (ETA / late-delivery), statsforecast (demand), **filterpy** (Kalman) | Small, fast, explainable |
| AI Copilot | **Claude API** (`claude-sonnet-5-5` for speed; `claude-opus-5-5` for hard reasoning) with tool use | Natural language → scenario JSON → run → explain. "Evidence-gated": it can propose but never apply without human approval. |

### Backend and data

| Concern | Choice |
|---|---|
| API | **FastAPI** (REST + WebSockets + SSE), Pydantic v2 strict models, uvicorn |
| Bus / cache | **Redis 7**: Streams (ingest bus, consumer groups), pub/sub → WebSocket fan-out, scenario-result cache |
| Database | **PostgreSQL 16 + PostGIS + TimescaleDB** (single `timescale/timescaledb-ha` image): master data, geo, telemetry hypertables |
| Workers | `multiprocessing` pool (hackathon) → **arq/Celery** workers (Level 4, autoscaled) |
| Wire format | JSON for REST. **msgpack** diffs over WebSocket at 5–10 Hz, interpolated client-side to 60 fps. |

### Frontend (appearance decides the win)

| Concern | Choice |
|---|---|
| Framework | **Vite + React 19 + TypeScript** |
| Styling | **Tailwind CSS v4 + shadcn/ui** (Radix), **Motion** (framer-motion) for transitions |
| Map engine | **deck.gl 9.4 + MapLibre GL JS v5** (globe projection, synced with deck.gl since 9.1; TerrainLayer on GlobeView in 9.4) via `react-map-gl` |
| Basemap | CARTO Dark Matter (free, attribution) or MapTiler (free key, 3D terrain and buildings). OpenFreeMap as keyless fallback. |
| Charts | **Apache ECharts** (fan charts, Sankey of material flow, gauges, Pareto scatter) |
| Topology view | `react-force-graph-3d` (cascade-failure animation) or React Flow |
| State | Zustand (live state), TanStack Query (REST) |
| Polish | `cmdk` (⌘K palette), `sonner` (toasts), animated number tickers, Lucide icons, Geist/Inter + JetBrains Mono (tabular numbers) |

### DevOps

GitHub Actions (ruff, mypy, pytest, vitest, Playwright, Docker build, Trivy, pip-audit) · Docker Compose (dev) · **k3s or managed K8s + Helm + HPA/KEDA** (Level 4) · Caddy (auto-HTTPS) · Prometheus + Grafana + Loki + Alertmanager → Telegram/Discord · k6/Locust load tests · Vercel/Cloudflare Pages for the frontend.

**Free resources:** the GitHub Student Developer Pack includes cloud credits (DigitalOcean/Azure) and a free `.tech` domain. Claim them on Day 0.

---

## 4. Architecture

```
   ┌────────────────────── DATA SOURCES ─────────────────────────┐
   │ Reality Emulator (SUMO+SimPy w/ hidden noise + attack inj.) │
   │ Open-Meteo (live weather)  AISStream (live ships, optional) │
   │ Supplier/ERP feeds (CSV/JSON)   DataCo dataset (backtest)   │
   └──────────────┬──────────────────────────────────────────────┘
                  │ HMAC-signed HTTP/WS batches
   ┌──────────────▼──────────────┐
   │  INGEST GATEWAY (FastAPI)   │  schema validation, auth, rate limit
   └──────────────┬──────────────┘
                  ▼  Redis Stream: telemetry.raw
   ┌─────────────────────────────┐    quarantine / DLQ
   │  TRUST WORKERS (9 layers)   ├──────────────► audit + Trust Center
   └──────────────┬──────────────┘
                  ▼  Redis Stream: telemetry.clean
   ┌─────────────────────────────┐         ┌────────────────────────┐
   │   TWIN STATE SERVICE        │◄───────►│  SIMULATION ORCHESTR.  │
   │ NetworkX graph + Redis live │         │ SimPy macro (live+MC)  │
   │ Timescale history + PostGIS │         │ SUMO micro (libsumo)   │
   └──────┬──────────────┬───────┘         └───────────┬────────────┘
          │              │                             │
   ┌──────▼─────┐ ┌──────▼──────┐   ┌──────────────────▼─────────┐
   │ OPTIMIZER  │ │ ML SERVICES │   │ FIDELITY / EVAL SERVICE    │
   │ OR-Tools   │ │ ETA, demand │   │ predictions vs actuals     │
   │ NetworkX   │ │ anomaly     │   │ attack benchmark, backtest │
   └──────┬─────┘ └──────┬──────┘   └──────────────┬─────────────┘
          └──────────────┼─────────────────────────┘
                  ┌──────▼──────────────────┐     ┌──────────────┐
                  │ API: REST + WS + SSE    │◄───►│ AI COPILOT   │
                  │ JWT/RBAC, /metrics      │     │ Claude tools │
                  └──────┬──────────────────┘     └──────────────┘
                         ▼
             React + deck.gl + MapLibre frontend
   Observability: Prometheus ─ Grafana ─ Loki ─ Alertmanager
```

**Key design decisions (write each one up as a 1-paragraph ADR in `docs/adr/` — judges love these):**

1. **Modular monolith first.** Ship one FastAPI app with clear modules, then split into containers for K8s in Level 4.
2. **Reality Emulator.** We have no real fleet, so a separately-seeded simulation with hidden perturbations *plays the physical world* and emits telemetry exactly like IoT devices. This gives ground truth for fidelity scoring and a safe place to inject attacks. Say this openly in the pitch. The ingest API also accepts real feeds (weather, AIS).
3. **SUMO for fidelity, SimPy for speed.** Live mode couples both. Monte Carlo runs macro-only, using SUMO-calibrated travel-time distributions, which gives ~1000× faster what-ifs.
4. **The twin validates the data.** The twin's own predicted state is used as a reference oracle to detect spoofing.
5. **Evidence-gated AI.** The LLM proposes and explains but can't mutate state. A human clicks Apply.

---

## 5. Repository layout

```
aegis-twin/
├── apps/web/                  # React + deck.gl frontend
├── services/api/app/
│   ├── ingest/  trust/  twin/  scenarios/  optimize/
│   ├── eval/    copilot/  auth/  chaos/   ws/
│   └── main.py
├── sim/
│   ├── macro/                 # SimPy engine: entities, processes, policies, MC runner
│   ├── micro/hyderabad/       # SUMO net.xml, vtypes, routes, sumocfg, runner.py
│   ├── coupling/              # orchestrator: time sync, spawn/arrive bridge
│   └── reality/               # reality emulator + attack injector
├── ml/                        # training scripts, notebooks, saved models
├── data/                      # nodes.json, lanes.json, skus.json, dataco/
├── infra/{docker,k8s,helm,grafana,prometheus,caddy}/
├── docs/{srs,architecture,dfd,wireframes,adr,pitch}/
├── tests/{unit,integration,e2e,load}/
└── .github/workflows/
```

---

## 6. Sequential build plan

The timeline assumes **~4 people and ~24 working days across the four levels**. If your windows are shorter, compress proportionally and follow the cut list in §8. Items marked **[CP]** are on the critical path.

### Team roles

| Role | Owns |
|---|---|
| **P1 Simulation lead** | SimPy engine, SUMO Hyderabad, coupling, reality emulator |
| **P2 Platform lead** | FastAPI, DB, ingest, trust layer, security, CI/CD, K8s, monitoring |
| **P3 Frontend lead** | Design system, map, all screens, animations, Director mode |
| **P4 Intelligence + story lead** | Optimizer, ML, Copilot, Fidelity Lab, SRS/docs, slides, video, pitch |

With 3 people, merge P4 into P1 and P2. With 2 people, one does the backend half and the other does the frontend and the story.

---

### Phase 0 — Kick-off and setup (Day 0, half a day)

1. Lock the name, tagline, colour palette and demo story (§2). Everything below serves the story.
2. Create the monorepo (§5), branch protection, PR template, `Makefile` (`make dev`, `make sim`, `make test`).
3. Install and verify on every laptop:
   - `pip install eclipse-sumo libsumo traci sumolib simpy networkx ortools fastapi` and run a 10-line libsumo hello-world.
   - `docker compose up` with Postgres/Timescale + Redis.
   - `npm create vite@latest web -- --template react-ts`, then add deck.gl, maplibre-gl, react-map-gl, tailwind, shadcn.
4. Get keys: MapTiler (basemap), Anthropic (Copilot), AISStream (optional). Open-Meteo needs no key.
5. Claim the GitHub Student Pack credits and domain.

**Exit:** everyone can run SUMO headless from Python, and the React map renders a dark India.

---

### Phase 1 — LEVEL 1: Idea, architecture, planning (Days 1–3)

The trick for this round is to present a **clickable high-fidelity prototype**, not paper wireframes. That prototype becomes your real frontend skeleton, so no work is wasted.

1. **SRS** (`docs/srs/SRS.md`, IEEE-830-lite):
   - Scope, personas (Supply-chain planner, Ops manager, Security analyst, Admin).
   - Functional requirements FR-1…FR-25, grouped by the 5 PNT1 features.
   - Non-functional targets. These are *targets* and must be validated later:

     | NFR | Target |
     |---|---|
     | Ingest throughput | ≥ 5,000 msgs/s on 4 vCPU |
     | Latency, ingest → UI | p95 < 1 s |
     | Monte Carlo (200 reps × 30 sim-days) | < 20 s on 8 cores |
     | UI frame rate | 60 fps with 2,000 vehicles |
     | Robustness | 0 crashes under 30% malformed or hostile input |
     | Availability | 99.5% |

2. **Architecture:** C4 Context + Container diagrams (§4), sequence diagrams for (a) telemetry ingest, (b) what-if scenario, (c) apply plan.
3. **DFDs:** Level 0 (system context) and Level 1 (ingest → trust → twin → sim/optimize → UI).
4. **ERD:** schema from §7.
5. **UI:** a static-data clickable prototype of all screens in §6.5, built in code with mock JSON. Take Figma screenshots for the document.
6. **Tech stack + roadmap:** Gantt of Phases 2–11, risk register (SUMO performance, time sync, demo failure), and a mitigation for each.
7. **Research slide:** cite the 6–8 papers and repos in §10, and show how each influenced a design choice.

**Exit / submission:** SRS PDF, architecture and DFD images, prototype URL (Vercel), roadmap.

---

### Phase 2 — World building: data and scenarios (Days 2–5) [CP]

1. **Network dataset** (`data/nodes.json`, `lanes.json`) using real coordinates:
   - **Ports:** JNPT/Nhava Sheva, Mundra, Chennai, Visakhapatnam, Kolkata/Haldia.
   - **Plants / suppliers:** Patancheru pharma API plant (Hyderabad), Chennai auto-parts, Ahmedabad textiles, Pune electronics, plus one overseas supplier via sea.
   - **DCs:** Hyderabad-Medchal (FMCG/e-com), Hyderabad-Shamshabad (pharma cold chain, near RGIA), Bengaluru, Nagpur (central hub), Delhi-NCR, Pune.
   - **Demand zones:** ~12 cities.
   - **Lanes:** mode (road/rail/sea/air), distance (OSRM or haversine × 1.3 detour factor), cost/unit-km, capacity, CO₂/t-km, lognormal lead-time parameters.
2. **SKUs:** 3 families.
   - Vaccines: perishable, cold chain, high value.
   - FMCG: high volume.
   - Electronics: high value, sea-imported.
3. **Demand:** base rate × weekly seasonality × festival spikes (Diwali +60%). Fit shape and noise from the DataCo dataset.
4. **SUMO Hyderabad** (`sim/micro/hyderabad/`):
   - Use `osmWebWizard.py` or `osmGet.py` + `netconvert`. Pick a bbox around the western and southern belt (Medchal – ORR – Gachibowli – Shamshabad – Patancheru), **not the whole ORR**, to stay fast.
   - Keep `highway.motorway` through `tertiary` only (`--keep-edges.by-type`) and use `--geometry.remove --ramps.guess --junctions.join`.
   - Truck `vType` (`vClass="truck"`, `maxSpeed=22`, length 12 m).
   - DC/plant locations as `parkingArea`s or named edges.
   - Background traffic via `randomTrips.py` so congestion looks real.
   - Test: 500 trucks + 3,000 cars must run faster than real time with libsumo.
5. **Scenario DSL** (Pydantic):

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

   Write 7 templates, one per type.

**Exit:** `python -m sim.macro.run --days 30` prints KPIs, and `python -m sim.micro.run` moves trucks across Hyderabad.

---

### Phase 3 — Simulation engines and coupling (Days 3–9) [CP]

**3a. SimPy macro engine (P1)**

- **Entities:**
  - `Supplier` (capacity `Resource`, failure/recovery process).
  - `Port` (berths `Resource`, customs delay).
  - `Warehouse` (per-SKU `Container` inventory, (s,S) or (R,Q) policy process, capacity).
  - `Lane` (transit process sampling lead time × disruption multiplier).
  - `DemandZone` (order generator).
- **Processes:** demand → fulfilment (nearest DC with stock, else backorder) → replenishment orders → shipments over lanes → receipt.
- **Disruption injector:** reads the scenario DSL, then disables nodes, multiplies lane times or scales demand.
- **Instrumentation:** an event log (every event a row, which gives an evidence trail per KPI) and KPIs: fill rate, OTIF, backorders, inventory days, cost (holding + transport + penalty), CO₂, and **TTS / TTR per node** (Simchi-Levi).
- **API:**
  - `Twin.run_until(t)`, `Twin.snapshot()`, `Twin.apply(event)`.
  - `monte_carlo(spec, n, seeds)` → percentile bands (P10/P50/P90) using `multiprocessing.Pool`.
  - Deterministic seeds everywhere.
- **Validation tests:**
  - A single-node (s,S) with Poisson demand matches the analytical fill rate within 2%.
  - EOQ sanity check.
  - Cross-check a 3-node toy against SupplyNetPy.
  - Put these in the SRS as "engine verification".

**3b. SUMO micro engine (P1)**

- `runner.py` owns a libsumo loop in its own process and steps at a configurable time-compression factor.
- **Commands (from a queue):**
  - `spawn_truck(shipment_id, from, to)` → `traci.vehicle.add` + route.
  - `close_road(edges)` → `traci.lane.setDisallowed(..., ["truck", ...])` or `edge.setMaxSpeed(0.1)`.
  - `reroute_all()` → `traci.vehicle.rerouteTraveltime`.
- **Outputs:** positions every N steps (FCD-like: id, lon/lat via `traci.simulation.convertGeo`, speed, angle), arrival events, and edge travel-time stats.
- **Calibration export:** per-corridor travel-time distributions feed the macro lanes inside Hyderabad.

**3c. Coupling orchestrator (P1)**

- **Clock:** the macro runs on `RealtimeEnvironment(factor=…)`. SUMO is stepped to match (e.g., 1 sim-minute per wall-second in demo mode).
- **Bridge:**
  - A macro shipment entering the Hyderabad region → micro `spawn_truck`.
  - A micro arrival → macro `receive(shipment)`.
  - A micro road closure → the macro lane-time multiplier updates from observed SUMO times.

**3d. Reality Emulator (P1 + P2)**

- A second instance of the coupled engines with a different seed plus hidden perturbations (unannounced delays, demand drift).
- It publishes realistic telemetry to the ingest API: GPS pings at 1 Hz per truck (with noise), warehouse stock counts, supplier ASN feeds, port status.
- **Attack injector:** GPS teleport, slow drift spoof, replayed or duplicated messages, missing fields, NaN/negative quantities, 10× inflated ASN, stale timestamps, feed blackout (drop X% of sources for Y minutes), and cascading node failure.
- Every injected attack gets a **ground-truth label**. This becomes your benchmark.

**Exit:** the coupled twin runs live, the reality emulator streams, and the numbers make sense.

---

### Phase 4 — Backend platform and ingestion (Days 5–10) [CP]

1. **DB migrations** (Alembic) for the §7 schema. Make telemetry a Timescale hypertable with a compression policy.
2. **Ingest:**
   - `POST /api/v1/ingest/{telemetry|inventory|supplier}` (batch, HMAC header) and `WS /ingest/stream`.
   - Pydantic strict validation, then XADD to `telemetry.raw`.
   - Target ≥ 5k msgs/s. Use batching and `orjson`.
3. **Twin-state service:** consumes `telemetry.clean`, updates the Redis live state (hashes per vehicle/node) and the NetworkX graph attributes, and writes to Timescale in batches.
4. **WebSocket fan-out:** `WS /ws/live` pushes msgpack diffs (positions, KPIs, alerts) at 5–10 Hz. `WS /ws/scenarios/{id}` pushes progress.
5. **REST:**
   - `GET /network`, `/nodes/{id}`, `/shipments`, `/kpis`
   - `POST /scenarios`, `GET /scenarios/{id}`, `POST /scenarios/{id}/optimize`, `POST /plans/{id}/apply`
   - `POST /chaos/inject` (admin only)
   - `GET /trust/*`, `/eval/*`
   - `/healthz`, `/readyz`, `/metrics`
6. **Scenario jobs:** a worker pool runs Monte Carlo and streams progress. Results are cached in Redis by `sha256(canonical spec JSON)`, so a repeated what-if returns instantly (this doubles as a Level 4 caching feature).

**Exit:** the reality emulator → ingest → twin → WebSocket pipeline works, and `wscat` shows live diffs.

---

### Phase 5 — Frontend MVP (Days 3–11, parallel, P3)

See §6.5 for the full screen spec. MVP order:

1. **Design system:** tokens, glass panel, KPI card, alert item, badge, and a map layer-toggle panel.
2. **Map shell:**
   - MapLibre v5 globe + deck.gl overlay (interleaved).
   - Camera presets `WORLD → INDIA → HYDERABAD` with `flyTo`.
3. **Control Tower** with live WebSocket data:
   - Nodes (ScatterplotLayer with pulse), lanes (ArcLayer), trucks (TripsLayer with trails) and inventory (ColumnLayer 3D bars).
   - KPI strip and alert feed.
4. **Scenario Lab v1:** pick a template → run → fan chart + KPI deltas.
5. **Client interpolation:** keep the last 2 positions per vehicle and lerp in `requestAnimationFrame` for smooth 60 fps even on 5 Hz data.

---

### Phase 6 — LEVEL 2 checkpoint: MVP (Day ~11)

**The demo must run end to end:** live map with trucks and stock → create "Chennai port closure" → Monte Carlo → results → see the impact on the map.

- Tag `v0.2` and **record a backup video** of the flow.
- Write a README with GIFs, architecture diagram and "how to run" (`docker compose up`).
- Submission: repo, video, running local demo, DB schema doc, API docs (FastAPI's auto `/docs`).

---

### Phase 7 — LEVEL 3a: Intelligence (Days 11–16)

**7.1 Optimizer and recommendation engine (P4)**

1. **Candidate generation** for a disruption:
   - **Reroute:** k-shortest multimodal paths (NetworkX `shortest_simple_paths`) on the graph with disrupted nodes/edges removed. Weight = cost + λ·time + μ·CO₂.
   - **Reallocate:** OR-Tools `SimpleMinCostFlow`. Supplies are surplus DCs and demands are the DCs projected to stock out (from Monte Carlo P50). Arc cost = transfer cost + delay penalty. For multi-SKU, use PuLP + HiGHS.
   - **Expedite:** switch critical SKUs to air.
   - **Buffer:** raise the safety stock for the duration.
2. **Evaluate** each candidate with Monte Carlo (N=200, common random numbers across plans for a fair comparison) → service level, cost, delay, CO₂, **CVaR₉₅ of lost sales**, TTS vs TTR.
3. **Rank** by Pareto front, then a weighted score (the user adjusts weights with sliders). Generate a human-readable explanation from the event log: *"Plan A keeps Shamshabad above safety stock by moving 1,200 vaccine units from Bengaluru, arriving in 14 h."*
4. **Apply:** `POST /plans/{id}/apply` pushes changes into the live twin (new routes, transfers). SUMO trucks reroute visibly.

**7.2 Network criticality and cascades (P4)**

- Betweenness centrality + a node-removal cascade simulation (Motter–Lai-style load redistribution on lanes) → "single points of failure" ranking and Risk Exposure Index per node (TTR-based, Simchi-Levi).
- Frontend: a 3D force graph where nodes turn red in sequence as the cascade spreads.

**7.3 ML models (P4)**

| Model | Approach | Report |
|---|---|---|
| ETA / delay | LightGBM on (lane, mode, distance, hour, weather, congestion from SUMO) | MAE and P90 interval coverage. Train on reality-emulator history + DataCo (`Days for shipping (real)` vs `scheduled`, `Late_delivery_risk`). |
| Demand forecast | statsforecast AutoETS per zone × SKU | Feeds the (s,S) reorder points |
| Disruption / anomaly detection | IsolationForest + robust z-score (MAD) on feeds | Optional upgrade inspired by the autoencoder + OCSVM + LSTM approach in arXiv 2309.14557 |

**7.4 AI Copilot (P4)**

- A right-side drawer with a chat that streams over SSE and renders tool calls as cards (e.g., "Running 500 simulations…").
- **Tools:** `get_network_state`, `find_at_risk_nodes`, `create_scenario(spec)`, `run_scenario(id)`, `optimize(id)`, `compare_plans(ids)`, `explain_kpi(kpi, node)`, `propose_apply(plan_id)`. The last one returns a confirmation button and the human must click it.
- Guardrails: tool outputs are data, it can't run SQL or shell, every claim links to evidence, and it has a strict JSON schema for scenario specs.

**7.5 Trust layer, full 9 layers (P2)** — see §6.4.

---

### Phase 8 — LEVEL 3b: Security, CI/CD, cloud deployment (Days 14–18)

1. **Auth:** OAuth2 password flow + JWT (short-lived access, refresh), argon2 hashing, **RBAC** roles:

   | Role | Can |
   |---|---|
   | viewer | Read |
   | planner | Run scenarios and apply plans |
   | security | Use the chaos console and quarantine |
   | admin | Everything |

   Clerk/Auth0 is acceptable if short on time.
2. **Device security:** per-device HMAC-SHA256 keys, timestamp + nonce replay protection (Redis SETNX with a TTL), key rotation endpoint.
3. **Transport and app security:**
   - Caddy with automatic HTTPS, strict CORS, security headers (CSP, HSTS).
   - Rate limiting (slowapi) and request size limits.
   - Parameterised SQL only (SQLAlchemy).
   - Secrets from env / GitHub secrets. Encrypt PII columns (pgcrypto) if you add any.
4. **Audit log:** every apply, chaos injection and login is written to `audit_log` and shown in the Trust Center.
5. **CI (GitHub Actions):** lint (ruff, eslint), type (mypy, tsc), tests (pytest with coverage, vitest), Playwright smoke test of the demo flow, Docker build, Trivy image scan, pip-audit / npm audit.
6. **CD:** on merge to `main`, build and push images to GHCR, then deploy.
   - **Backend:** SSH + `docker compose pull && up -d` on a cloud VM (DigitalOcean/Azure credits). Later, `helm upgrade` in Level 4.
   - **Frontend:** Vercel.
7. **Public URL** with the free `.tech` domain and HTTPS. Put it on the first slide.

**Exit / submission:** a live HTTPS URL with login, a green CI badge and a security section in the docs.

---

### Phase 9 — Evaluation framework: Fidelity Lab (Days 15–19)

This is PNT1's "production-ready evaluation" requirement. Treat it as a first-class feature, not a report.

1. **Shadow-mode prediction logging:** every N minutes the twin writes predictions to the `predictions` table: ETA per active shipment, stock level per node/SKU at t+6/12/24 h, stockout yes/no within 24 h, each with P10/P90. When reality arrives, fill `actual`.
2. **Metrics** (live, in the Fidelity Lab):

   | Metric | Measures |
   |---|---|
   | MAPE / MAE / RMSE | ETA and stock-level predictions |
   | P10–P90 interval coverage | Calibration. The target is ~80%. Plot a reliability diagram. |
   | Stockout event precision / recall / F1 | Event prediction |
   | Wasserstein distance | Simulated vs observed travel-time distributions per corridor |
   | Drift alarm | Rolling MAPE > threshold → auto-recalibrate lane lead-time parameters (Bayesian update) → show the "self-healing twin" event in the UI |

3. **Attack benchmark (red team):** run the labelled attack suite (≥ 500 injected attacks + clean traffic), then report detection precision, recall, F1, false-positive rate and mean time-to-detect per attack type. Show a confusion matrix.
4. **Decision value (counterfactual):** 50 random disruption scenarios × {no action, naïve rule, AEGIS recommendation}, with common random numbers. Report the % reduction in lost sales, cost and CO₂ with confidence intervals. **This is your headline slide number.**
5. **Real-data backtest:** the ETA/late-delivery model on the held-out DataCo split (MAE, AUC). This answers the question "accuracy against actual outcomes" with real data.
6. **One-click evaluation report:** `GET /eval/report` renders HTML/PDF with every metric, chart, seed and engine version. This makes results reproducible and auditable.

---

### Phase 10 — LEVEL 4: Scalability, reliability, enterprise readiness (Days 18–22)

1. **Containerise per service:** `api`, `ingest`, `trust-worker`, `twin-state`, `sim-worker`, `sumo-runner`, `web`. Use multi-stage slim images, a non-root user and healthchecks.
2. **Kubernetes** (k3s on the VM, or DOKS/GKE Autopilot) with a Helm chart:
   - Deployments and Services, Ingress (Traefik/NGINX + cert-manager).
   - **HPA** on `api`/`ingest` (CPU), **KEDA** on `sim-worker` scaled by Redis Stream / queue length.
   - PodDisruptionBudgets, liveness/readiness probes, resource requests/limits.
   - Redis and Postgres as StatefulSets or managed services.
3. **Performance:**
   - Redis caching: scenario results (spec-hash), `/network`, and KPI snapshots with a 1–2 s TTL.
   - Timescale continuous aggregates for KPI history, plus indexes on `(vehicle_id, ts DESC)` and GiST on geometry.
   - Pagination, gzip/brotli, WebSocket backpressure (drop stale frames, not the connection).
4. **Load tests:** k6 for ingest (ramp to find the ceiling, report msgs/s at p95 < 1 s) and Locust for API users. Put the real numbers in the pitch.
5. **Observability:**
   - Prometheus metrics: ingest rate, reject/quarantine rate per reason, trust-score distribution, sim job latency, queue depth, WebSocket clients, p95 API latency, SUMO step time.
   - A Grafana "AEGIS Ops" dashboard plus Loki logs and OpenTelemetry traces (API → worker).
   - **Alertmanager** → Telegram/Discord: ingest stall, quarantine spike (attack in progress), MAPE drift, pod crash-loop.
6. **Reliability:**
   - Circuit breakers around external APIs (weather, AIS), which fall back to cached data.
   - Bulkheads: the sim worker crashing never affects ingest.
   - Graceful degradation: the twin runs in predictive mode if a feed dies.
   - Nightly `pg_dump` backup.
7. **Chaos drill (demo moment):** `kubectl delete pod -l app=trust-worker` live. Grafana shows the dip and recovery while the UI stays up.

**Exit / submission:** Helm chart, Grafana screenshots, load-test report and alert demo.

---

### Phase 11 — Polish, pitch and rehearsal (continuous; final 3 days dedicated)

1. **Director mode:** press `F` and a scripted, deterministic replay of the whole §2 story runs (camera flights, events, attacks) from a recorded seed. This is your insurance if Wi-Fi or the cloud dies.
2. **Backup video** (1080p, 3 min), stored locally and on YouTube (unlisted).
3. **Deck (10 slides):**
   1. Hook and problem (₹/$ cost of disruptions)
   2. Solution in one sentence + the zoom GIF
   3. Live demo
   4. Architecture
   5. Two-scale SUMO × SimPy insight
   6. Trust layer
   7. Fidelity numbers
   8. Scale / K8s
   9. Research grounding
   10. Team and ask
4. **Q&A prep:** write answers to the likely judge questions.
   - "Is the data real?" (Reality emulator + real weather/AIS/OSM/DataCo, and why.)
   - "How do you know the twin is accurate?" (Fidelity Lab.)
   - "What if the LLM hallucinates?" (Evidence-gated, human approval.)
   - "Why SimPy and SUMO both?"
   - "How does it scale?"
   - "What's novel?" (Two-scale coupling + twin-as-oracle spoof detection.)
5. **Rehearse 5+ times out loud** with a timer. Test **on the actual projector resolution**, since dark UIs can wash out, so bump contrast if needed. Pre-cache map tiles and keep a phone hotspot ready.

---

### 6.4 Trust layer (adversarial resilience), in pipeline order

| # | Layer | Technique | Catches |
|---|---|---|---|
| 1 | Schema | Pydantic v2 strict, ranges, enums. Missing optional fields imputed from last-known + twin prediction and flagged `imputed`. | Incomplete / garbage payloads |
| 2 | Authenticity | HMAC-SHA256 per device, nonce + timestamp window | Forged sources, replay attacks |
| 3 | Temporal | Monotonic timestamps, max clock skew, dedupe by message ID | Stale / replayed / duplicated data |
| 4 | Physics | Speed ≤ 120 km/h (truck), acceleration bounds, haversine teleport check (Δd/Δt) | GPS teleport spoofing |
| 5 | Map-matching | Distance to nearest SUMO road edge (`sumolib net.getNeighboringEdges`) | Off-road spoofed coordinates |
| 6 | State estimation | Per-vehicle constant-velocity **Kalman filter**. Innovation Mahalanobis gating (χ² test). | Subtle drift spoofing (see GPS-IDS, arXiv 2405.08359) |
| 7 | Twin oracle | Compare reported position with the twin's predicted position on the planned route; flag if outside the P99 envelope | Coordinated spoofs that are physically plausible |
| 8 | Feed anomaly | IsolationForest + MAD z-score on ASN quantities, lead times and prices. Cross-source reconciliation (ASN vs warehouse receipts). | Corrupted / poisoned supplier feeds |
| 9 | Source reputation | Beta-reputation trust score per source (0–1). Low trust leads to down-weighting and then quarantine. | Persistently bad actors |

**Blackout handling:** if a source goes silent past its SLA, mark the entity `PREDICTED`. The twin dead-reckons it from simulation, and the UI shows a widening uncertainty cone plus a "last seen" badge. The system never crashes. Bad messages go to `quarantine` (a DLQ with a reason code) and never enter the twin state.

**Cascading node failures:** the graph cascade model (§7.2) plus auto-failover. When a DC fails, the orchestrator reassigns its demand zones to the next-best DC and replans.

---

### 6.5 Frontend spec ("win on appearance")

**Visual language: dark mission control.**

- **Colours:**
  - Background `#070B14`, panel `rgba(15,22,38,0.72)` with `backdrop-blur-xl` and a 1px `rgba(255,255,255,0.08)` border.
  - Semantic: **cyan `#22D3EE` = healthy flow**, **amber `#F59E0B` = at risk**, **red `#EF4444` = disrupted**, **violet `#A78BFA` = AI recommendation**, **green `#34D399` = recovered**.
  - Use colour only for meaning, never decoration.
- **Typography:** Geist/Inter for the UI, JetBrains Mono with tabular numbers for all figures.
- **Motion:** 200–400 ms eased transitions. Camera `flyTo` 2–3 s. Numbers count up. Disrupted nodes pulse. Arcs carry animated particles showing flow direction and volume.
- **Restraint:** a maximum of 4 KPI cards on screen, and everything else is one click away. Judges should read the screen in 3 seconds.

**Screens:**

1. **Intro (5 s, skippable):** the 3D globe rotates, then flies to India, then network arcs draw in one by one, then the tagline fades in.
2. **Control Tower (home):**
   - Full-bleed map with a left layer panel (Nodes, Lanes, Trucks, Ships, Inventory bars, Weather, Risk heatmap).
   - Top KPI strip: Fill rate, OTIF, At-risk shipments, ₹ at risk.
   - Right alert feed and a bottom **timeline scrubber with Past | Now | Future**. Dragging into the future shows predicted state as ghosted, translucent layers.
   - Click a node to open a slide-over with inventory gauges per SKU, inbound/outbound shipments, and TTS vs TTR.
3. **Scenario Lab:**
   - Disruption cards (Port closure, Cyclone, Road flood, Demand spike, Supplier failure, Strike, Data blackout) that you **drag onto the map**. A cyclone draws an animated polygon.
   - Click Run to see a progress ring (N/500 runs).
   - **Split-screen synced maps: Baseline vs Mitigated.** Below them, ECharts fan chart (P10–P90 band) of stock over time, a Pareto scatter of plans, and plan cards with KPI deltas and **Apply**.
4. **City Twin (Hyderabad 3D):**
   - Pitched 3D view with extruded buildings (MapLibre fill-extrusion), SUMO trucks as TripsLayer with glowing trails, and cars as faint dots.
   - Click a road to close it (a flood icon appears), and trucks reroute in real time with the old path shown red-dashed and the new path green.
5. **Trust & Security Center:**
   - Chaos Console buttons, source trust gauges and a live quarantine log (reason codes).
   - On the map, a spoofed truck shows as a **red ghost** with the Kalman-estimated **green true position** linked by a dashed line.
   - Attack timeline and detection counters.
6. **Fidelity Lab:** predicted vs actual line charts, reliability diagram, attack confusion matrix, decision-value bar chart, drift status and the "Download evaluation report" button.
7. **Network Graph:** a 3D force graph of the topology with the cascade animation and criticality ranking.
8. **Ops (Level 4):** an embedded Grafana panel, pod status and the load-test summary.
9. **Copilot drawer:** available on every screen via ⌘J. Streaming chat with tool-call cards and Apply buttons.

**Demo ergonomics:**

- Keyboard shortcuts: `1–8` switch screens, `D` triggers the demo disruption, `C` opens chaos, `F` starts Director mode, `⌘K` opens the command palette.
- A loading skeleton everywhere, and never a blank screen or spinner longer than 1 s during the demo.

**Performance budget:** 60 fps with 2,000 trucks + 3,000 cars (deck.gl handles this easily with binary attributes). Test on a mid-range laptop and the projector.

---

## 7. Data model (core tables)

| Table | Key columns |
|---|---|
| `nodes` | id, type (supplier/port/plant/dc/zone), name, `geom` (PostGIS point), capacity, attrs jsonb, status |
| `lanes` | id, from_id, to_id, mode, distance_km, cost_per_unit, capacity, co2_per_tkm, lt_mu, lt_sigma, status |
| `skus` | id, family, unit_value, perishable, shelf_life_h, cold_chain |
| `inventory` (hypertable) | ts, node_id, sku_id, on_hand, on_order, backorder, source, trust |
| `orders` | id, zone_id, sku_id, qty, created_ts, promised_ts, fulfilled_ts, status |
| `shipments` | id, lane_id, sku_id, qty, vehicle_id, status, depart_ts, eta_pred, eta_p10, eta_p90, arrive_ts |
| `telemetry` (hypertable) | ts, vehicle_id, lat, lon, speed, heading, source_id, sig_ok, trust, flags[] |
| `sources` | id, type, hmac_key_hash, trust_score, last_seen, sla_s |
| `quarantine` | id, ts, source_id, payload jsonb, reason_code, layer |
| `disruptions` | id, spec jsonb, start_ts, end_ts, status |
| `scenarios` | id, spec jsonb, spec_hash, n_reps, status, results jsonb, created_by |
| `plans` | id, scenario_id, actions jsonb, kpis jsonb, score, applied_at, applied_by |
| `predictions` | id, entity, metric, made_ts, horizon_h, value, p10, p90, actual, actual_ts |
| `attack_labels` | id, ts, type, target, injected_by (benchmark ground truth) |
| `audit_log` | id, ts, user_id, action, target, details jsonb |
| `users`, `roles` | RBAC |

---

## 8. Scope control: cut list (MoSCoW)

If time runs short, cut from the bottom and **never** cut from MUST.

| Priority | Items |
|---|---|
| **MUST** | SimPy macro twin + India map with arcs, trucks and inventory · 1 end-to-end disruption scenario with Monte Carlo + at least 2 ranked plans + Apply · GPS-spoof + corrupted-feed + blackout detection visible in the UI · Fidelity metrics (MAPE + attack F1 + decision value) · Director mode + backup video |
| **SHOULD** | SUMO Hyderabad city twin with live rerouting and coupling · AI Copilot · Chaos Console · public HTTPS deploy with auth + CI/CD · Grafana |
| **COULD** | Live AIS ships · 3D force-graph cascade view · K8s autoscaling with KEDA · DataCo backtest · drift self-recalibration · split-screen compare |
| **WON'T** | Real IoT hardware, GNN models (mention SupplyGraph as future work), mobile app |

---

## 9. Risks and mitigations

| Risk | Mitigation |
|---|---|
| SUMO network too big or slow | Smaller bbox, filter road types, libsumo (not TraCI), cap at 500 trucks, background traffic at low density |
| Macro/micro time-sync bugs | A single orchestrator owns the clock. Integration test: a shipment spawned in macro must arrive in micro and be received back in macro. |
| Live demo fails (Wi-Fi, cloud) | Director mode runs fully local, plus a backup video, cached tiles and a hotspot |
| Scope creep | §8 cut list. Freeze features 3 days before the final and spend the rest on polish. |
| "Is this real?" skepticism | Be upfront about the reality emulator. Show real OSM, weather, AIS and DataCo integrations. Report measured numbers only. |
| LLM latency or cost during demo | Cache the Copilot responses for the scripted demo question, and use a fast model for tool routing |
| Projector washes out dark UI | Test early and keep a high-contrast theme toggle ready |

---

## 10. Research and repos that informed this plan

**Simulation**

- Eclipse SUMO: [docs](https://sumo.dlr.de/docs/index.html), [Libsumo](https://sumo.dlr.de/docs/Libsumo.html), [OSM import / osmWebWizard](https://sumo.dlr.de/docs/Networks/Import/OpenStreetMap.html), [Routing and rerouting](https://sumo.dlr.de/docs/Simulation/Routing.html), [Docker image](https://sumo.dlr.de/docs/Developer/Docker.html), [PyPI eclipse-sumo 1.27.1](https://pypi.org/project/eclipse-sumo/)
- [A SUMO-Based Digital Twin for Evaluation of Conventional and EV Networks (arXiv 2507.10280)](https://arxiv.org/pdf/2507.10280): SUMO-as-twin methodology and a real-vs-sim accuracy check
- [sidewalklabs/sumo-web3d](https://github.com/sidewalklabs/sumo-web3d): the TraCI → WebSocket frame-diff → browser pattern we reuse (with deck.gl instead of three.js)
- [sumo3Dviz (arXiv 2604.19194)](https://arxiv.org/pdf/2604.19194): 3D visualisation of SUMO trajectories
- [SimPy real-time environment](https://simpy.readthedocs.io/en/latest/api_reference/simpy.rt.html) and [dynamic-des](https://github.com/jaehyeon-kim/dynamic-des) (live parameter updates for SimPy twins)
- [SupplyNetPy (arXiv 2607.09745)](https://arxiv.org/html/2607.09745) / [GitHub](https://github.com/SupplyChainSimulation/SupplyNetPy): SimPy supply-network library validated against AnyLogistix, EOQ and newsvendor. This is our validation reference.
- [SupplyTwin-Simulation](https://github.com/mercer14k/SupplyTwin-Simulation): validation-first ingestion, deterministic seeds, evidence-gated AI (patterns we borrow)

**Supply-chain resilience theory**

- [Ivanov et al., *Digital Supply Chain Twins: Managing the Ripple Effect…*](https://link.springer.com/chapter/10.1007/978-3-030-14302-2_15): the ripple effect as the conceptual foundation
- Simchi-Levi, TTS / TTR / Risk Exposure Index: [MIT News](https://news.mit.edu/2022/companies-use-mit-research-identify-respond-supply-chain-risks-0615), [summary](https://blogs.lt.vt.edu/bit5494/2015/06/14/time-to-survive-tts-versus-time-to-recover-ttr/)
- [Critical node identification and resilience against cascading failures (PLOS One)](https://journals.plos.org/plosone/article?id=10.1371%2Fjournal.pone.0344005)
- [Disruption Detection for a Cognitive Digital Supply Chain Twin (arXiv 2309.14557)](https://arxiv.org/abs/2309.14557): autoencoder + OCSVM + LSTM
- [SupplyGraph GNN benchmark (arXiv 2401.15299)](https://arxiv.org/abs/2401.15299): future-work slide

**Adversarial resilience**

- [GPS-IDS: anomaly-based GPS spoofing detection (arXiv 2405.08359)](https://arxiv.org/html/2405.08359v1): physics model + ML
- [Secure localisation under GPS spoofing with decomposition Kalman filter (Sci. Rep.)](https://www.nature.com/articles/s41598-025-32863-5)

**Evaluation**

- [Digital Twin Counterfactual Framework (arXiv 2604.01325)](https://arxiv.org/html/2604.01325): a fidelity hierarchy for validating simulated outcomes

**Optimisation**

- [OR-Tools min-cost flow](https://or-tools.github.io/docs/pdoc/ortools/graph/python/min_cost_flow.html), [Imperial notebook](https://transport-systems.imperial.ac.uk/tf/notebooks/n12_minimum_cost_flow_ortools.html)

**Frontend**

- [deck.gl What's New (v9.4, Sep 2026)](https://deck.gl/docs/whats-new), [TripsLayer](https://deck.gl/docs/api-reference/geo-layers/trips-layer), [deck.gl + MapLibre](https://deck.gl/docs/developer-guide/base-maps/using-with-maplibre)

**Data**

- [Open-Meteo](https://open-meteo.com/en/docs) (free, no key)
- [AISStream](https://aisstream.io/) (free live AIS over WebSocket; server-side only)
- [DataCo Smart Supply Chain dataset](https://www.kaggle.com/datasets/shashwatwork/dataco-smart-supply-chain-for-big-data-analysis)
- [Hyderabad warehousing hubs](https://addressadvisors.com/blog/top-5-warehousing-hubs-in-hyderabad-for-logistics)

**Winning**

- [JetBrains: notes from the judging table (2026)](https://blog.jetbrains.com/ai/2026/06/how-to-win-a-hackathon-notes-from-the-judging-table/)
- [Devpost: advice from 5 judges](https://info.devpost.com/blog/hackathon-judging-tips)

---

## 11. Immediate next steps (today)

1. Confirm the team size, the level deadlines and whether the final round is on-site. Then convert §6 into a dated schedule.
2. Run Phase 0. The two things most likely to go wrong later are SUMO + libsumo on each laptop and the deck.gl globe rendering, so test those first.
3. Start the Level 1 clickable prototype (P3) and the SRS (P4) in parallel, while P1 builds the Hyderabad SUMO network and P2 scaffolds the monorepo and Docker Compose.
