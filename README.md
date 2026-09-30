# AEGIS Twin — Logistics Network Digital Twin (PNT1)

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

Not currently hosted. The Level-1 demo used a temporary Vercel deployment, which has since expired. To host it permanently, deploy it yourself (see [Deploying the prototype](#deploying-the-prototype)), or run it locally.

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

- **Macro twin** (SimPy): 28 real-coordinate nodes, 49 lanes (OSRM road distances, sea routes via Malacca), 3 SKU families, demand fitted from DataCo with a Diwali +60% spike. `--scenario cyclone --compare` shows the scenario next to the baseline, using common random numbers.
- **Micro twin** (SUMO 1.27, libsumo): the Hyderabad western/southern belt, 10,670 edges, 12 DC/plant hubs as parkingAreas, background traffic. 500 trucks + 3,000 cars run at **71.6× real time** (`python -m sim.micro.bench`).
- **Scenario DSL** (Pydantic v2): 7 templates in `sim/scenarios/templates/`, validated by `python -m sim.scenarios.validate`.
- Tests: `.venv/bin/python -m pytest` (38 tests).

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
sim/macro/           SimPy macro twin: network, demand model, engine, `python -m sim.macro.run`
sim/micro/           SUMO Hyderabad micro-twin: build, libsumo runner, `run`, `bench`
sim/scenarios/       Scenario DSL (Pydantic), 7 templates, JSON Schema
tests/               pytest suite (network, demand, DSL, macro, micro)
data/                Network (nodes, lanes, skus, sourcing), demand params + festivals, OSRM cache
scripts/gen_mock.py  Seeded mock data for the prototype (incl. a toy Monte Carlo)
docs/                srs · architecture · dfd · erd · wireframes · roadmap · pitch · adr
tools/               Doc build tooling: Mermaid render, PDF build, prototype screenshots
PLAN.md              Full build plan (Levels 1–4)
```
