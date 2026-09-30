# AEGIS Twin — Tech stack, roadmap and risk register

**Level 1 deliverable · 30 September 2026**

**Dating assumption:** Day 1 = 1 Oct 2026, with ~4 people and ~24 working days. Once the organisers confirm the level deadlines, shift the bars; the order and dependencies stay the same.

![Roadmap Gantt, Phases 2–11](gantt.png)

## 1. Phase plan

Owners: **P1** simulation lead · **P2** platform lead · **P3** frontend lead · **P4** intelligence + story lead. [CP] = critical path.

| Phase | Dates (Day) | Owner | Scope | Exit criterion |
|---|---|---|---|---|
| **1** Idea, architecture, planning | 1–3 Oct (D1–3) | all | SRS, C4, sequences, DFDs, ERD, clickable prototype, roadmap, research slide | ✅ This submission |
| **2** World building [CP] | 2–5 Oct (D2–5) | P1 | `data/nodes.json`, `lanes.json`, `skus.json` (done early in Phase 1); demand model; SUMO Hyderabad net; 7 scenario-DSL templates | `python -m sim.macro.run --days 30` prints KPIs; `python -m sim.micro.run` moves trucks |
| **3** Engines + coupling [CP] | 3–9 Oct (D3–9) | P1 | SimPy macro engine, MC runner, SUMO runner, coupling orchestrator, Reality Emulator + attack injector | Coupled twin runs live; emulator streams; engine verification tests pass (SRS §8); NFR-3 benchmarked |
| **4** Backend + ingestion [CP] | 5–10 Oct (D5–10) | P2 | Alembic schema (§7), ingest API, Redis Streams, twin-state service, WS fan-out, REST, scenario jobs + spec-hash cache | Emulator → ingest → twin → WS pipeline visible in `wscat`; first NFR-1 / NFR-2 numbers |
| **5** Frontend MVP | 3–11 Oct (D3–11) | P3 | Swap the prototype's mock JSON for live WS/REST; client interpolation; Scenario Lab v1 on real results | Control Tower on live data at 60 fps |
| **6** Level 2 checkpoint | 11 Oct (D11) | all | End-to-end: live map → Chennai closure → MC → results → impact on map; tag `v0.2`; backup video; README | Level 2 submission |
| **7** Intelligence | 11–16 Oct (D11–16) | P4, P2 | Optimizer (reroute, min-cost-flow, expedite, buffer), Pareto/CVaR ranking, cascades + REI, LightGBM ETA, demand forecast, Copilot, full 9-layer trust | Ranked plans + Apply move trucks; each attack type caught by its layer |
| **8** Security + CI/CD + cloud | 14–18 Oct (D14–18) | P2 | JWT + RBAC, HMAC devices, Caddy TLS, rate limits, audit log, GitHub Actions (lint/type/test/Playwright/Trivy), GHCR, VM deploy, Vercel | Public HTTPS URL with login; green CI badge |
| **9** Fidelity Lab | 15–19 Oct (D15–19) | P4 | Shadow predictions, live metrics, drift recalibration, red-team benchmark (≥ 500 attacks), counterfactual decision value, DataCo backtest, `/eval/report` | Measured headline numbers for the pitch |
| **10** Scale + reliability (Level 4) | 18–22 Oct (D18–22) | P2 | Per-service containers, Helm, HPA + KEDA, Prometheus/Grafana/Loki/Alertmanager, k6/Locust, circuit breakers, backups, chaos drill | Helm chart; Grafana screenshots; load-test report; pod kill self-heals live |
| **11** Polish + pitch | continuous; 22–24 Oct (D22–24) | P3, P4 | Director mode, backup video, 10-slide deck, Q&A prep, 5+ rehearsals on the projector | Feature freeze D21; final pitch D24 |

**Dependencies:** P3 needs the P2 network data. P4 needs P3's `Twin.snapshot()` API. P7 optimisation needs the P3 Monte Carlo runner. P9 needs P3's Reality Emulator (ground truth) and P7's trust layer. P10 needs the P8 containers.

## 2. Tech stack

| Layer | Choice |
|---|---|
| Simulation | SimPy 4.1 (macro, real-time env), Eclipse SUMO 1.27 via libsumo (micro), NetworkX |
| Optimisation / ML | OR-Tools (min-cost flow, VRP), PuLP + HiGHS, LightGBM, statsforecast, scikit-learn, filterpy |
| AI | Claude API with tool use (`claude-sonnet-5-5` routing, `claude-opus-5-5` reasoning), evidence-gated |
| Backend | FastAPI, Pydantic v2 strict, uvicorn, orjson, msgpack; Redis 7 (Streams, pub/sub, cache); PostgreSQL 16 + PostGIS + TimescaleDB |
| Frontend | Vite, React 19, TypeScript, Tailwind v4, deck.gl 9.4, MapLibre 5, ECharts 6, Motion, Zustand, cmdk, sonner, react-force-graph-3d |
| DevOps | GitHub Actions, Docker, Docker Compose, k3s + Helm, HPA/KEDA, Caddy, Prometheus, Grafana, Loki, Alertmanager, k6, Locust, Vercel |

## 3. Risk register

Likelihood (L) and impact (I): H/M/L. Each risk has an owner, an early-warning trigger and a mitigation already scheduled in the plan.

| ID | Risk | L | I | Owner | Trigger (early warning) | Mitigation | Contingency |
|---|---|---|---|---|---|---|---|
| **R-1** | **SUMO performance:** network too big or slow to run faster than real time with the target vehicle count | M | H | P1 | D3 benchmark: 500 trucks + 3,000 cars slower than 1× real time | Small bbox (western + southern belt, not the whole ORR); `--keep-edges.by-type` motorway→tertiary; `--geometry.remove --junctions.join`; **libsumo** in-process (not TraCI sockets); cap live trucks at 500; low-density `randomTrips` background; SUMO in its own process | Pre-record SUMO FCD for the demo corridor and replay it; Monte Carlo is already macro-only (ADR-0003) |
| **R-2** | **Time sync:** macro and micro clocks drift, so shipments arrive twice, never, or out of order | M | H | P1 | Handshake test flaky; arrivals outside ±1 step | A **single orchestrator owns the clock**; SUMO is stepped to the macro `RealtimeEnvironment` (e.g. 1 sim-min per wall-second); a command queue with sim-time stamps; CI integration test (spawn in macro → arrive in micro → received in macro); monotonic IDs make receipts idempotent | Fall back to macro-only live mode with SUMO as visual replay |
| **R-3** | **Demo failure:** Wi-Fi, cloud, tile CDN or LLM down on stage | M | H | P3 | Any rehearsal needing a network retry | **Director mode** (`F`) replays the whole story locally from a recorded seed; backup 1080p video (local + unlisted YouTube); pre-cached map tiles; cached Copilot answers for the scripted question; phone hotspot; laptop runs the full `docker compose` stack offline | Switch to the video at the first sign of trouble, and narrate live |
| R-4 | Scope creep | H | M | P4 | Any MUST item behind by > 1 day | MoSCoW cut list (`PLAN.md` §8); feature freeze D21 | Cut from COULD, then SHOULD; never MUST |
| R-5 | Judges doubt the data is real | M | M | P4 | Q&A rehearsal | Say it upfront: Reality Emulator (ADR-0002) + real OSM, weather, AIS, DataCo; measured numbers only | Show the DataCo backtest slide |
| R-6 | LLM latency or cost during the demo | M | M | P4 | p95 Copilot latency > 4 s | Cache scripted-question responses; fast model for tool routing | Director mode plays the cached transcript |
| R-7 | Projector washes out the dark UI | M | M | P3 | First projector test | Test at projector resolution early; high-contrast theme toggle; bump the ink colour | Light theme |
| R-8 | Ingest misses NFR-1 on 4 vCPU | M | M | P2 | First k6 run < 3,000 msg/s | Batching, orjson, `XADD` pipelining, uvicorn workers = vCPU, trust workers scaled by KEDA | Report the measured ceiling honestly, with the profile |
| R-9 | Team availability (exams, illness) | M | M | all | Missed stand-up | Pairing across roles; docs in-repo; a Director-mode demo is always runnable | Re-plan via the cut list |

## 4. Definition of done (every phase)

Code merged via PR with CI green. Tests for new logic. The demo path still runs in Director mode. Numbers quoted anywhere are reproducible from a seed.
