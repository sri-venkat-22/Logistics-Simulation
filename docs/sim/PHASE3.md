# Phase 3 — Simulation engines and coupling

**AEGIS Twin · Days 3–9 · critical path** · status: **done** (30 Sep 2026)

Exit criterion: *the coupled twin runs live, the reality emulator streams, and the numbers make sense.*

| Criterion | Command | Result |
|---|---|---|
| SimPy macro engine with entities, KPIs, TTS/TTR, `run_until` / `snapshot` / `apply` | `python -m sim.macro.run --days 30` | ✅ 30 days in **0.07 s**; fill 100.0%, OTIF 99.5%; every KPI backed by event-log rows |
| Monte Carlo P10/P50/P90, deterministic seeds (NFR-3: 200 reps × 30 days < 20 s on 8 cores) | `monte_carlo(RunSpec(...), n=200)` | ✅ **2.8 s** on 8 cores; the pool and a single process give bit-identical bands |
| Engine verification (SRS §8) | `python -m sim.macro.verify` | ✅ V1 analytic (s,S) fill **0.03%** off · V2 EOQ minimum **exact** · V3 SupplyNetPy **Δ 0.01 pp** |
| SUMO worker in its own process, command queue, time compression | `sim/micro/process.py` | ✅ lock-step `step_to` and free-running at any factor (600× measured) |
| Corridor calibration → macro lanes | `python -m sim.micro.calibrate` | ✅ 84/84 trucks; 5 Hyderabad lanes recalibrated (`calibration.json`) |
| Coupled twin (FR-5 handshake) | `python -m sim.coupling.run --hours 30 --flood` | ✅ 11 macro→SUMO→macro round trips, max receipt lag **50 s** (sync 60 s), 0 fallbacks, 30 sim-h in 4 s |
| Reality Emulator streaming + labelled attacks | `python -m sim.live --hours 48` · `python -m sim.reality.run` | ✅ 2.8 M signed messages in 48 sim-h (≈46 k msg/s generated); HTTP sink verified against a local ingest stub |
| Red-team benchmark (≥ 500 labelled attacks) | `python -m sim.reality.bench` | ✅ **561 attacks**, 10 types, 219 k messages; byte-identical on regeneration |
| Tests | `python -m pytest` · `python -m pytest -m slow` | ✅ **70 + 3 slow** passed |

All commands assume `.venv/bin/python`. Install the new dependency with `.venv/bin/pip install -r requirements.txt` (adds `supplynetpy` for V3).

---

## 3a. SimPy macro engine (`sim/macro/`)

| Module | What it holds |
|---|---|
| `entities.py` | **Supplier**: production lines as a `simpy.Resource` (processor sharing across the jobs on a line); hourly output × production factor; ships at 18:00 and when a job completes; random failure/recovery (MTBF plant 45 d, supplier 60 d, overseas 90 d; lognormal MTTR 10/12/24 h). **Port**: berths as a `Resource` (attrs `berths`); anchorage while closed; berth service (lognormal, mean 8 h) ÷ capacity factor; customs (mean 18 h) for imports. **Warehouse**: one `simpy.Container` per SKU with a storage capacity, a FIFO backlog, on-order + yard, a policy process per SKU, and a daily dispatch budget. **Lane**: transit = lognormal lead time (or SUMO-calibrated handling + drive) × live multiplier + overlap with disruptions; hands city legs to the coupler. **DemandZone**: one order per zone × SKU per day, or a renewal process (toys). |
| `policies.py` | `SSPolicy(s, S)`, `RQPolicy(R, Q)` (R, nQ), `DynamicSSPolicy(z, cover)` (the forecast-driven default); periodic (`review_h` = 24) or continuous (`review_h` = 0) review; backorders or lost sales |
| `disruptions.py` | Disruption injector: scenario DSL → `Effect` (node / production / dispatch factors, lane multipliers, demand multipliers, SUMO edges) |
| `engine.py` | `Twin`: fulfilment from the **nearest DC with stock** (serving DC, then backups by distance, else backorder at the serving DC); replenishment → supplier job → multi-leg shipment → receipt; event log; stock-out episodes; `run_until(t)`, `snapshot()`, `apply(event)`; `RealtimeEnvironment` when `realtime_factor` is set |
| `kpis.py` | Fill rate, OTIF, backorders, inventory days, cost (transport **booked at dispatch**, holding, penalty, ordering), CO₂, in-transit value, **TTS/TTR per disruption and per node**; `EVIDENCE` maps each KPI to the event rows behind it (`Twin.evidence("fill_rate", dc=...)`) |
| `montecarlo.py` | `RunSpec` (Pydantic, `spec_hash()`), `monte_carlo(spec, n, seeds)` → P10/P50/P90 for scalar KPIs, daily on-hand/backlog bands per DC × SKU, stock-out probability, TTS bands |
| `resilience.py` | Simchi-Levi stress test: TTS, TTR, exposure and Risk Exposure Index per node |
| `verify.py` | Engine verification V1–V3 (below) |

**Live API.** `apply()` accepts `scenario` (relative starts count from *now*), `disruption_end`, `lane_multiplier`, `node_status`, `demand_multiplier`, `set_path` (plan actions), `order`, and `receive` (micro arrival). Bad events raise `ValueError`. `snapshot()` is JSON: node status (supplier lines and queues, port berths/anchorage/customs), inventory with current s/S, every shipment with lat/lon/progress, lane multipliers, active effects and KPIs to date.

**Deterministic seeds everywhere.** Every random stream is keyed by name (`demand/zone/sku`, `lane/id`, `port/id`, `failure/id`, `city/shipment`), so runs with the same seed share common random numbers across scenarios. `run_until` in steps gives exactly the same events as one run (tested).

**TTS / TTR.** For each disruption, TTR is the time until the node is back at full capacity. TTS is the time from its start to the first stock-out at a DC × SKU that depends on it (a pair whose sourcing path uses the node or lane, or which serves the affected zone). It is `None` if the network survives the window. `exposed` = TTS < TTR.

### Results (30 days from 15 Oct 2026, seed 42, SUMO-calibrated Hyderabad lanes)

| Run | Fill | OTIF | Vaccine | FMCG | Electronics | Stock-out episodes | Cost | CO₂ | TTR | TTS | Exposed |
|---|---|---|---|---|---|---|---|---|---|---|---|
| baseline | 100.0% | 99.5% | 100.0% | 100.0% | 100.0% | 1 | ₹564.9 L | 346.6 t | — | — | — |
| cyclone | 100.0% | 99.5% | 100.0% | 100.0% | 100.0% | 1 | ₹564.4 L | 346.6 t | 5.0 d | > window | no |
| data_blackout | 100.0% | 99.5% | 100.0% | 100.0% | 100.0% | 1 | ₹564.9 L | 346.6 t | 0.0 d | > window | no |
| demand_spike | 100.0% | 99.6% | 100.0% | 100.0% | 100.0% | 0 | ₹570.6 L | 352.0 t | 7.0 d | > window | no |
| port_closure | 100.0% | 99.5% | 100.0% | 100.0% | 100.0% | 1 | ₹564.4 L | 346.6 t | 5.0 d | > window | no |
| road_flood | 100.0% | 99.5% | 100.0% | 100.0% | 100.0% | 1 | ₹564.9 L | 346.6 t | 1.5 d | > window | no |
| strike | 100.0% | 99.5% | 100.0% | 100.0% | 100.0% | 1 | ₹564.8 L | 346.6 t | 2.0 d | > window | no |
| supplier_failure | 100.0% | 99.5% | 100.0% | 100.0% | 100.0% | 1 | ₹564.8 L | 346.6 t | 3.0 d | > window | no |
| stress: Patancheru outage 7 d | 99.3% | 94.8% | 86.2% | 100.0% | 100.0% | 5 | ₹609.1 L | 346.6 t | 7.0 d | 4.2 d | **yes** |
| stress: Ahmedabad outage 10 d | 60.5% | 87.1% | 100.0% | 54.8% | 100.0% | 11 | ₹680.8 L | 344.8 t | 10.0 d | 7.9 d | **yes** |
| stress: Chennai port closed 21 d | 99.9% | 98.6% | 100.0% | 100.0% | 98.6% | 4 | ₹570.9 L | 346.6 t | 21.0 d | 18.1 d | **yes** |

The baseline's single stock-out episode is one hour of FMCG at Delhi during the Diwali ramp. Phase 2's finding still holds: at template severity the shocks are absorbed. Costs are higher than in Phase 2 (₹551.5 L) because transport is now booked when each leg is dispatched, and ports add berth and customs time.

**Monte Carlo (NFR-3).** 200 replications × 30 days of the Chennai cyclone took **2.76 s** on 8 cores (Apple silicon) and 11.0 s in one process; both gave identical bands. Fill-rate band P10/P50/P90 = 99.25 / 100 / 100%. P(stock-out attributable to the cyclone) = 4%, with P50 TTS 26 d (after the port reopens: the queue of held ships).

### Simchi-Levi stress test (`docs/sim/resilience.json`)

Each node fully down for 30 days gives TTS. Each node down for its TTR gives the impact and the REI (normalised to the worst node).

| Node | TTS | TTR | Exposed | REI | Impact |
|---|---|---|---|---|---|
| SUP_AHMEDABAD_TEX | 8.4 d | 14 d | **yes** | 1.00 | ₹730.9 L |
| DC_DELHI | 0.3 d | 5 d | **yes** | 0.66 | ₹482.4 L |
| PLANT_PATANCHERU | 4.8 d | 10 d | **yes** | 0.32 | ₹232.0 L |
| DC_PUNE | 0.5 d | 5 d | **yes** | 0.27 | ₹200.1 L |
| DC_BLR | 0.4 d | 5 d | **yes** | 0.27 | ₹199.1 L |
| DC_HYD_SHAMSHABAD | 0.5 d | 5 d | **yes** | 0.05 | ₹37.1 L |
| DC_HYD_MEDCHAL | 1.4 d | 5 d | **yes** | 0.05 | ₹33.7 L |
| SUP_SHENZHEN | 25.6 d | 21 d | no | 0.02 | ₹15.6 L |
| PORT_CHENNAI / JNPT / VIZAG | 18.4 / 21.4 / 23.4 d | 7 d | no | 0.00 | — |
| DC_NAGPUR | 22.5 d | 5 d | no | 0.00 | — |
| PORT_MUNDRA, PORT_KOLKATA, SUP_CHENNAI_AUTO, SUP_PUNE_ELEC | > 30 d | 7–14 d | no | 0.00 | — |

What this tells the team: the single-sourced FMCG supplier is the dominant risk. Every DC is exposed within hours of closing, because backups only take orders they can fill in full. Ports are buffered by roughly 18 days of electronics cover.

## Engine verification (SRS §8) — `python -m sim.macro.verify`

Full report with seeds and curves: [`engine_verification.json`](engine_verification.json).

| Check | Set-up | Result | Pass |
|---|---|---|---|
| **V1** single-node (s,S), Poisson demand | λ = 20/day, L = 2 d, (s,S) = (45, 80), daily review, backorders, 20,000 days after 60 warm-up. Analytic fill = E[min(D, (Y − D_L)⁺)]/λ, Y from the Markov chain of the post-order position | simulated **86.94%** vs analytic **86.97%** (rel. error 0.03%) | ✅ ≤ 2% |
| **V2** EOQ | D = 240/day, K = ₹5,000, h = ₹1/unit/day, L = 1 d, (R,Q) continuous review, Q ∈ 600…3000 step 150, each measured over exactly 200 cycles | EOQ = 1,549; simulated minimum at **Q = 1,500** (nearest grid point); max deviation from K·D/Q + h·(Q/2 + SS + step/2) = **0.00%**; no stock-outs | ✅ |
| **V3** 3-node toy vs **SupplyNetPy 0.1.12** | infinite supplier → distributor (s,S) = (40, 90), continuous review, L = 48 h → Poisson unit demand 1/h, lost sales; 20 seeds × 20,000 h in each engine | fill **85.80% vs 85.81%** (Δ 0.01 pp) · mean on-hand 22.3 vs 22.3 (Δ 0.3%) · orders 342.0 vs 342.6 (Δ 0.2%) | ✅ |
| FR-5 macro ↔ micro handshake | coupled run, 30 sim-h (`tests/test_coupling.py`) | every shipment that enters SUMO is received back in macro; receipt lag ≤ one sync step (60 s); SUMO spawn time = macro time exactly | ✅ |
| SUMO calibration | Wasserstein-1 between the macro prior and SUMO drive times per corridor (below) | reported per lane | — |

**V1 found a real engine bug.** `simpy.Container.put` raises the level synchronously, but on-order stock was only decremented after the `yield`. A review in the same instant as a receipt therefore counted the delivery twice and skipped the reorder (simulated fill 66% vs 87%). The fix moves the bookkeeping before the put, and yard stock is counted separately while storage is full.

## 3b. SUMO micro engine (`sim/micro/`)

- **`runner.py` (MicroTwin)**:
  - `spawn_truck(vid, from, to, depart=…, meta=…)` uses an absolute depart time, so the coupling can insert trucks at the exact macro instant.
  - A truck **arrives when it pulls into its destination parking area**, not after it leaves the network.
  - It also offers `close_road` / `reopen_road` / `reroute_all`, FCD positions (lon/lat via `simulation.convertGeo`, speed, angle, edge, shipment metadata), observed per-edge truck travel times (Welford), and `corridor_time` / `corridor_route`.
- **`process.py` (MicroProcess)**: libsumo in its own OS process, with commands over a queue:
  - `spawn_truck`, `close_road`, `reopen_road`, `reroute_all`, `step_to`, `corridor_times`, `corridor_route`, `positions`, `edge_stats`, `run`, `pause`, `set_factor`, `status`, `stop`.
  - Lock-step (`step_to`, for coupling) or free-running at a time-compression factor (measured: 1 wall-second ≈ 600 sim-seconds at `factor=600`).
  - One process per world, so the twin and the Reality Emulator each get their own SUMO.
- **`calibrate.py`**:
  - 84 trucks drive the five in-city corridors through the busy first hour of background traffic.
  - Each macro lane gets `lead time = 4 h handling + LogNormal(drive)`, in `sim/micro/hyderabad/calibration.json`. The macro twin loads it by default (`calibration="auto"`).

| Lane | Route | n | Prior drive (38 km/h) | SUMO mean | P10–P90 | σ(log) | Lane mean: prior → calibrated | W1 |
|---|---|---|---|---|---|---|---|---|
| L015 | Patancheru → Shamshabad DC | 12 | 82 min | 40.6 min | 40.5–40.8 | 0.05* | 5.40 → 4.68 h | 41 min |
| L016 | Patancheru → Medchal DC | 12 | 61 min | 26.7 min | 26.2–27.2 | 0.05* | 5.00 → 4.45 h | 34 min |
| L047 | Medchal DC → Shamshabad DC | 12 | 131 min | 68.0 min | 61.5–71.0 | 0.06 | 6.20 → 5.13 h | 62 min |
| L030 | Medchal DC → Z_HYD (6 consumer hubs) | 24 | 49 min | 35.1 min | 18.0–57.5 | 0.38 | 4.80 → 4.59 h | 14 min |
| L031 | Shamshabad DC → Z_HYD (6 consumer hubs) | 24 | 38 min | 34.2 min | 23.3–47.6 | 0.23 | 4.60 → 4.57 h | 4 min |

\* floored at 0.05. **Finding:** trucks on the ORR run close to free-flow in SUMO, because the generated background traffic is light there. The macro prior of 38 km/h is about twice as slow as SUMO on the ORR corridors. The ORR σ is therefore under-estimated until real congestion data (or denser background demand) is added.

## 3c. Coupling orchestrator (`sim/coupling/`)

- **One clock.** The macro twin owns time (`RealtimeEnvironment(factor)` in demo mode; `--realtime 60` = 1 sim-minute per wall-second, measured 3.0 s wall for 3 sim-minutes).
  - A sync process steps SUMO in lock-step every 60 sim-seconds: SUMO t = (macro h − t₀) × 3600.
  - Trucks are spawned with an absolute SUMO depart time, so they enter SUMO at the exact macro instant.
  - Arrivals return at the next sync (lag ≤ 60 s; measured max 50 s).
- **Bridge.** Coupled lanes are those with both ends in the micro-twin (L015, L016, L030, L031, L047), plus inbound road/air lanes into Hyderabad nodes and outbound road lanes. Inbound and outbound legs enter or exit through the **gateway hub** nearest to where the straight route crosses the SUMO bbox; air lanes use RGIA Air Cargo.
  - One truck per truck-load (VAX 400, FMCG 1,000, ELEC 1,500 units; up to 4).
  - The last truck's arrival triggers `Twin.apply({"type": "receive"})`.
  - A watchdog falls back to macro timing if SUMO loses a truck.
- **Closures → macro multipliers.** A `road_flood` effect with `sumo_edges` closes those edges in SUMO. On coupled lanes, its static slow-down is replaced by `(lane time + city time × (r − 1)) / lane time`, where r is the SUMO corridor ratio: routed at once, then an EWMA of observed truck times. Reopening restores the lanes to 1.0.

**Finding (fidelity):** the `road_flood` template assumes the flood slows L006/L015/L047 by **6.7×**. With the two flooded ORR edges closed, SUMO reroutes onto the parallel service road for **+1.3%** drive time, which is under +0.2% of the whole lane once loading is included. The coupled twin uses SUMO's number. For a demo-worthy flood, the template needs a wider closure (e.g. the whole Shamshabad ORR interchange); the tests show the mechanism by cutting successive fastest routes until the corridor is more than 30% slower.

## 3d. Reality Emulator (`sim/reality/`)

- **World.** A second Twin (seed 1042) plus its own SUMO worker, with hidden perturbations recorded in `truth()` but never published:
  - every road lane secretly 0–15% slower;
  - Poisson (1/day) unannounced incidents: a random road/rail lane ×1.5–3 for 4–24 h;
  - a per-zone daily demand random walk (+0.3% ± 2%/day).
- **Telemetry** (`schemas.py`: strict Pydantic envelope + payloads, reusable by the Phase 4 ingest):
  - **GPS at 1 Hz per truck**: SUMO trucks inside Hyderabad, and national trucks interpolated along their lane. Each ping has 4 m noise, HDOP and 0.3% multipath jumps.
  - Warehouse cycle counts every 15 min, with count noise.
  - Supplier ASNs: one per truck-load, with ETA.
  - Port berth / queue / customs status every hour.
- **Signing and sinks.** Each message is signed with HMAC-SHA256 under a per-device key derived from a master key. Sinks: memory, JSON lines (deterministic gzip), and **HTTP**, which posts batches of ≤ 500 to `/api/v1/ingest/{telemetry|inventory|supplier}` with `X-AEGIS-Publisher / Timestamp / Nonce / Signature` headers (verified in tests against a local ingest stub).
- **Attack injector** (`attacks.py`). Every attack gets a ground-truth label with the exact `(msg_id, occurrence)` pairs it touched, and the trust layer + reason code expected to catch it (SRS Appendix B).
  - Spoofed data is re-signed with the compromised device's key, so detection must be content-based.
  - Replays and duplicates are byte-identical copies, labelled by occurrence.
  - Attacks whose target stops reporting are labelled `skipped`, never counted.
  - The `data_blackout` DSL template maps to a labelled blackout.

| Attack | Mechanism | Expected |
|---|---|---|
| gps_teleport | next 1–5 pings jump 300–800 km | L4 PHYSICS_TELEPORT |
| gps_drift | position drifts at 0.5–3 m/s for 5–20 min | L6 KALMAN_GATE |
| replay | 5–30 messages published 1–30 min earlier, re-sent verbatim | L3 REPLAY_NONCE |
| duplicate | a source's next 1–10 messages sent twice | L3 DUPLICATE_ID |
| missing_fields | a required payload field dropped | L1 SCHEMA_MISSING |
| nan_negative | a numeric field → NaN, ∞ or negative | L1 SCHEMA_RANGE |
| inflated_asn | the next ASN claims 10× the quantity | L8 ASN_OUTLIER |
| stale_timestamp | timestamps 2–48 h old | L3 STALE_TS |
| blackout | 10–40% of sources silent for 5–20 min | L9 SLA_SILENT |
| cascade_failure | a node fails in the reality twin, then 1–2 downstream DCs after 1–6 h each | L7 TWIN_ENVELOPE |

### The benchmark (`data/benchmark/`)

`python -m sim.reality.bench` covers simulated day 5 (20 Oct 2026, 00:00–24:00), after a silent warm-up. It uses attack seed 7 and world seed 1042, with national GPS every 5 s and city GPS at 1 Hz.

| | |
|---|---|
| Messages published | **218,770** (205,931 clean, 12,839 malicious) + 23,003 withheld by blackouts |
| By kind | GPS 216,991 · stock 1,632 · port 129 · ASN 18 |
| Attacks | **561 injected** / 600 scheduled (39 found no live target) |
| Per type (done / messages) | teleport 52/153 · drift 72/9,924 · replay 58/1,065 · duplicate 72/359 · missing fields 82/483 · NaN/negative 76/420 · inflated ASN 10/10 · stale 71/425 · blackout 49/23,003 dropped · cascade 19 (physical) |
| Reproducibility | regenerating gives byte-identical files; SHA-256 in `manifest.json` (telemetry `d6e3cbcb…`; regenerated in Phase 4 after the national-GPS geometry fix) |

`labels.jsonl`, `truth.json` and `manifest.json` are committed. `telemetry.jsonl.gz` (18 MB) is git-ignored; regenerate it in about 14 s and compare its hash with the manifest.

**Scoring rule for Phase 9:** a message is malicious if its `(msg_id, occurrence)` appears in a label with an action other than `dropped`. Blackouts are scored per silenced source (`SLA_SILENT`).

**Caveat:** supplier ASNs are lumpy (a dispatch every few hours), so the day holds only 18 ASNs and 10 inflated-ASN attacks. Phase 9 should report that class with a wide confidence interval, or add a longer ASN-only campaign.

## Live side by side — `python -m sim.live --hours 48`

The twin (seed 42) and reality (seed 1042 + hidden perturbations) ran in lock-step, each with its own SUMO worker. Over 48 sim-hours (60 s wall), both completed their macro→SUMO→macro handshakes (18/18), and reality streamed **2.8 M** signed messages (1 Hz national GPS). With no feedback yet, the open-loop mean on-hand gap between twin and reality stayed at **0.7–4.8%**. That is the error the Phase 4 ingest + Phase 7 trust layer must close.

## Reproduce

```bash
.venv/bin/pip install -r requirements.txt
```

```bash
.venv/bin/python -m sim.macro.verify
```

```bash
.venv/bin/python -m sim.macro.resilience --out docs/sim/resilience.json
```

```bash
.venv/bin/python -m sim.micro.calibrate
```

```bash
.venv/bin/python -m sim.coupling.run --hours 30 --flood
```

```bash
.venv/bin/python -m sim.reality.bench
```

```bash
.venv/bin/python -m sim.live --hours 48 --every 6
```

```bash
.venv/bin/python -m pytest && .venv/bin/python -m pytest -m slow
```
