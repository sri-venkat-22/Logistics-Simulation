# AEGIS Twin — Logistics Network Digital Twin (PNT1)

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

Vercel: **https://temporary-quick-neon-romn6e8.vercel.app** (deployed 30 Sep 2026)

This is an anonymous *temporary* Vercel deployment: it expires 60 minutes after creation unless it is claimed into a Vercel account. For a permanent URL, claim it or redeploy with `vercel login` (see [Deploying the prototype](#deploying-the-prototype)), then update this line.

> Every number in the prototype is **seeded mock data** (`scripts/gen_mock.py`) and every screen says so with a *PROTOTYPE · MOCK DATA* badge. Geography is real: node coordinates are real sites, and Hyderabad road geometry comes from OpenStreetMap via OSRM.

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
data/                Reference network: nodes.json, lanes.json, skus.json (+ generator, cached OSRM routes)
scripts/gen_mock.py  Seeded mock data for the prototype (incl. a toy Monte Carlo)
docs/                srs · architecture · dfd · erd · wireframes · roadmap · pitch · adr
tools/               Doc build tooling: Mermaid render, PDF build, prototype screenshots
PLAN.md              Full build plan (Levels 1–4)
```
