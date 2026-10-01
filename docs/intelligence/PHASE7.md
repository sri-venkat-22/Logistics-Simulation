# Phase 7 — LEVEL 3a: Intelligence

**AEGIS Twin · Days 11–16** · status: **done** (1 Oct 2026)

Every number below comes from a script in this repository and is reproducible from its seed; the JSON each script writes sits next to this file or in `docs/ml`, `docs/trust` and `docs/sim`.

| Deliverable | Where | Result |
|---|---|---|
| 7.1 Optimiser + recommendation engine | `sim/optimize/optimizer.py`, API in `services/api/app/scenarios.py` | 7 candidate kinds, each evaluated with Monte Carlo on common seeds. Pareto front, weighted score (sliders) and event-log explanations. On a 21-day Chennai port closure the top plan cuts **CVaR₉₅ shortfall by 89 %** (₹264.6 L → ₹27.8 L) for +₹5.7 L cost |
| 7.2 Criticality + cascades | `sim/optimize/criticality.py` | Betweenness, flow share, a Motter–Lai cascade and the Simchi-Levi REI combine into a single-point-of-failure ranking. Top SPOF: Ahmedabad supplier (87 % of served demand cut off). 3-D cascade animation in the Network Graph |
| 7.3 ML | `ml/eta.py`, `ml/forecast.py`, `ml/anomaly.py` | ETA (LightGBM quantiles): twin MAE 6.76 h vs 7.09 h schedule, P10–P90 coverage 78 %. DataCo MAE 1.03 d vs 1.29 d, late-delivery AUC 0.759. **AutoETS + calendar WAPE 13.4 %** vs 29.3 % for the planning model; used for reorder points it lifts fill **71.9 % → 91.7 %**. ASN anomaly: precision 1.0, 0 false positives |
| 7.4 AI Copilot | `services/api/app/copilot.py`, `apps/web/src/components/LiveCopilot.tsx` | Claude Opus 5.5 with strict tools, streamed over SSE, or an offline planner on the same tools. Proposals need a human Apply; nothing mutates the twin |
| 7.5 Trust layer, all 9 layers | `services/api/app/trust_layers.py`, `pipeline.py`, [`docs/trust/TRUST.md`](../trust/TRUST.md) | Red-team benchmark: **536 / 561 attacks detected (95.5 %)**, message precision 0.959, false-positive rate 0.19 %, 46k msgs/s on one core. Live: 0 quarantines in 102,574 clean emulator messages |
| Tests | `tests/test_optimize.py`, `test_trust.py`, `test_ml.py`, `test_copilot.py` | Part of the 119-test suite (see Phase 8) |

---

## 7.1 Optimiser and recommendation engine

```bash
.venv/bin/python -m sim.optimize.report
```

**Candidates** (`optimizer.candidates`) are generated for the DC × SKU pairs that depend on the disruption:

| Kind | How | Twin action |
|---|---|---|
| Reroute (same suppliers) | `networkx.shortest_simple_paths` on the lane graph with closed nodes removed. Weight = cost + λ·value-of-time·hours + μ·₹2/kg·CO₂, disrupted lanes slowed by their multiplier. Cold-chain SKUs only through cold-chain DCs | `set_route` |
| Switch sourcing | The same routing with any source of the SKU | `set_route` |
| Transfer stock | OR-Tools `SimpleMinCostFlow` in a benefit formulation. Donors (P(stock-out) < 5 %) give up to half of their lowest P10 stock. DCs that Monte Carlo projects short (P50 / P90 backlog) receive −BIG per unit. Arc cost = transfer cost + stock-out penalty × hours. Multi-hop, decomposed into DC → DC transfers | `transfer` |
| Expedite | Time-dominated routing (λ = 25) with air allowed, for vaccine and electronics, plus air transfers | `set_route`, `transfer` |
| Buffer | +0.8 on the safety factor z of the affected families | `policy_buffer` |
| Combined | Reroute + transfer, and the full response (reroute + transfer + buffer) | all |

The engine gained `set_route` (any contiguous lane sequence from a source of the SKU to the DC, validated) and `transfer` (immediate withdrawal, shipment over the lanes, receipt booked against the destination's on-order). `RunSpec` gained `routes` and `transfers`. Four **transfer-only lanes** (L050–L053, the reverse of existing DC ↔ DC corridors) were added to `data/`. Sourcing paths are byte-identical (verified), and the Reality Emulator skips them in its hidden perturbations, so every earlier result reproduces.

**Evaluation.** Each plan runs N Monte Carlo replications on the scenario's own seeds (common random numbers). The metrics are service (P50 fill), cost, CO₂, OTIF, delay, **CVaR₉₅ of the shortfall value** (the mean of the worst 5 % of runs), and TTS vs TTR.

**Ranking** (`optimizer.rank`) marks the Pareto front on (service ↑, cost ↓, CO₂ ↓, CVaR ↓). The weighted score uses planner weights, re-ranked server-side by `GET /scenarios/{id}/plans?service=&risk=&cost=&co2=` behind the sliders in the Scenario Lab.

**Explanations** come from one evidence replication with the event log on; the evidence rows travel with the plan. Real output for the Chennai 21-day closure:

> Full response (reroute + transfer + buffer): moves 58 electronics units from Hyderabad-Medchal to Hyderabad-Shamshabad, arriving in 5 h; moves 528 electronics units from Hyderabad-Medchal to Bengaluru-Hoskote, arriving in 31 h; reroutes 2 replenishment flows via Hyderabad-Shamshabad, Visakhapatnam Port; raises safety stock for electronics. It keeps Bengaluru-Hoskote electronics in stock where doing nothing stocks out at day 21.5. P50 fill 100.0 % vs 99.8 %, cost +5.7 L, CO2 +1.3 t, CVaR95 shortfall -236.8 L.

**Apply** (`POST /plans/{id}/apply`, planner role, audited) pushes routes, transfers and buffers into the live twin. A single failing action (for example, a donor DC that closed since the plan was made) is reported, not fatal.

The approved plan also goes **to the fleet**: the API publishes it on the Redis stream `plan.commands`, and the Reality Emulator (`--chaos-redis`) executes it in reality.

- Transfers and new routes through Hyderabad become **SUMO trucks on the new city corridors**. The transfer-only lane L053 (Shamshabad → Medchal) is coupled too.
- The emulator acknowledges dispatched shipments in the hash `plan.dispatch`, served at `GET /plans/dispatched`.
- The Control Tower and Scenario Lab draw those trucks and their trails in AI violet, and the tooltip names the plan.

Live check: applying *Full response* on the 21-day Chennai closure sent two electronics transfers out of Medchal as SUMO trucks, at 75–79 km/h through Secunderabad. `tests/test_reality.py::test_applied_plan_reaches_the_fleet_and_its_trucks_drive_in_sumo` covers both directions.

Measured (n = 100 per plan, 30 days, seeds 42–141; [`optimizer.json`](optimizer.json)):

| Scenario | Best plan | Fill P50 | CVaR₉₅ shortfall | Cost |
|---|---|---|---|---|
| Chennai port closed 21 d | Full response (reroute via Vizag + transfer from Medchal + buffer) | 99.85 → **100.00 %** | ₹264.6 L → **₹27.8 L** | +₹5.7 L |
| Patancheru plant down 7 d | Buffer (+0.8 z, vaccine) | 99.33 → 99.35 % | ₹120.8 L → ₹117.3 L | −₹0.9 L |
| Ahmedabad supplier down 10 d | Buffer (+0.8 z, FMCG) | 62.2 → **65.0 %** | ₹399.9 L → ₹377.2 L | −₹8.6 L |

The single-source cases are honest results. Every vaccine DC depends on Patancheru and every FMCG DC on Ahmedabad, so there is no donor and no alternative source. The optimiser correctly offers only buffering, and the gap motivates dual sourcing. The 5-day templates are absorbed by this network at 100 % fill ([`docs/world/scenario_results.md`](../world/scenario_results.md)), so the stress cases are the meaningful benchmarks.

## 7.2 Network criticality and cascades

```bash
.venv/bin/python -m sim.optimize.criticality
```

- **Flow model.** Each DC × SKU replenishment path carries the DC's base daily demand, and each serving lane carries its zone's demand.
- **Motter–Lai cascade.** Lane capacity is C = (1 + α)·L₀ + α·(physical capacity − L₀). The failed node's flows reroute onto the shortest surviving paths (replenishment from any source of the SKU; serving via the next backup DC). Lanes pushed above C fail, and the process repeats until stable.
- **Unserved demand** counts zones with no serving lane or served by a DC whose replenishment is cut.
- **SPOF score** = 0.4 REI + 0.3 cascade unserved share + 0.2 flow share + 0.1 betweenness.

| # | Node | SPOF | REI | Unserved if it fails |
|---|---|---|---|---|
| 1 | SUP_AHMEDABAD_TEX | 0.835 | 1.00 | 87 % |
| 2 | DC_DELHI | 0.594 | 0.66 | 35 % |
| 3 | DC_PUNE | 0.341 | 0.27 | 30 % |
| 4 | DC_BLR | 0.308 | 0.27 | 21 % |
| 5 | DC_HYD_MEDCHAL | 0.182 | 0.05 | 8 % |

API: `GET /network/criticality?alpha=` and `GET /network/cascade/{node}?alpha=`. The Network Graph screen animates the cascade step by step (failed nodes and overloaded lanes turn red) with a tolerance slider.

## 7.3 ML models

```bash
.venv/bin/python -m ml.eta && .venv/bin/python -m ml.forecast && .venv/bin/python -m ml.anomaly
```

**ETA / delay** (LightGBM quantile regression, P10 / P50 / P90):

| Model | Data | Result (held-out, time split) |
|---|---|---|
| Twin | 19,690 shipment legs from 360 days of the Reality Emulator (hidden lane bias, hidden incidents, announced weather). Features: lane, mode, distance, schedule, hour, weekday, announced weather multiplier, SUMO calibration | MAE **6.76 h** vs 7.09 h for the weather-adjusted schedule. P10–P90 coverage **78.2 %** (target ≈ 80 %). Road 2.71 vs 2.82 h, rail 15.7 vs 16.7 h, sea 79.6 vs 83.4 h |
| DataCo (real) | 180,519 orders; features: shipping mode, scheduled days, market, region, category, segment, month / weekday / hour (no leaking columns); last 20 % by order date held out | Days-for-shipping MAE **1.031 d** vs 1.285 d scheduled. P10–P90 coverage 93.8 %. Late-delivery **AUC 0.759** vs 0.701 |

Models are saved in `ml/models/` and served at `GET /ml/eta?lane=&weather=` and `GET /ml/eta/shipments`. The DataCo CSV is git-ignored (96 MB, CC BY 4.0); its result is kept in `docs/ml/eta.json`.

**Demand forecast → (s,S).** History is the Emulator's realised demand, including its hidden drift, from 15 Aug; the test covers 15 Oct → 11 Nov, the Diwali ramp. WAPE over 36 zone × SKU series:

| Seasonal naive | AutoETS | **AutoETS + festival calendar** | Planning model (DataCo fit, blind to drift) |
|---|---|---|---|
| 28.3 % | 24.4 % | **13.4 %** (14.7 % on festive days) | 29.3 % |

`Twin.forecaster` lets the dynamic (s,S) policies take their means from a forecaster. The same reality ran twice over the test window:

| Reorder points from | Fill | Stock-out episodes | Cost |
|---|---|---|---|
| Planning model | 71.9 % | 16 | ₹854 L |
| **AutoETS + calendar** | **91.7 %** | **4** | **₹743 L** |

**Feed anomaly (L8).** 666 clean ASNs from a fresh seed, plus 2,664 corrupted copies:

| Detector | Precision | Recall | False positives |
|---|---|---|---|
| Deployed L8 rule (one ASN per truck, so over-capacity is impossible; upper-tail MAD z; ETA before ship time; IsolationForest with z > 3) | **1.000** | 0.749 | **0 %** |

Recall is 100 % for ×10 and ETA-before-ship, 99.7 % for ×3, and 0 % for ×0.2. An under-reported ASN looks like a partial truck-load, so it is undetectable from quantity alone. Phantom stock is caught instead by stock reconciliation (`RECON_MISMATCH`).

## 7.4 AI Copilot

- **Endpoint.** `POST /api/v1/copilot/chat` (viewer+) streams SSE events: `text`, `tool_call`, `tool_result`, `proposal`, `done` / `error`. `GET /api/v1/copilot/status` reports the engine.
- **Engine `claude`.** Claude Opus 5.5 (`claude-opus-5-5`) runs a streamed manual tool loop:
  - Tool inputs are validated again with pydantic before running (`eager_input_streaming`); invalid input becomes an `is_error` tool result.
  - Adaptive thinking runs at medium effort, and prompt caching covers the frozen system prompt + tool list.
  - Server-side refusal fallbacks (`fallbacks="default"`) are on; `refusal` and `max_tokens` stop reasons are handled.
  - It is enabled when Anthropic credentials are configured.
- **Engine `offline`.** A deterministic planner for the common requests (what-ifs from natural language, at-risk nodes, network state, KPI explanations, plan comparison) drives the same tools. The demo, the tests and CI run without a key.
- **Tools.** Eight, each a strict schema: `get_network_state`, `find_at_risk_nodes`, `create_scenario`, `run_scenario`, `optimize`, `compare_plans`, `explain_kpi`, `propose_apply`.
- **Guardrails (ADR-0005).**
  - Tools run with the requester's role (what-ifs need planner).
  - `propose_apply` returns a proposal card and never applies it. The planner clicks Apply with their own token, and it is audited.
  - There is no SQL or shell. Tool results are data (the system prompt says so), and every quoted number comes from a tool result.

## 7.5 Trust layer — all 9 layers

See [`docs/trust/TRUST.md`](../trust/TRUST.md) for the layer design, how the Kalman gate was tuned on the labelled data, and per-attack results.

```bash
.venv/bin/python -m services.api.app.trust_bench
```

| Attack | Detected | Messages caught | TTD p50 | Caught by |
|---|---|---|---|---|
| GPS teleport | 52 / 52 | 153 / 153 | 0 s | L4 PHYSICS_TELEPORT |
| GPS drift | 60 / 72 | 6,165 / 9,924 | 20 s | L6 KALMAN_GATE |
| Replay | 58 / 58 | 1,065 / 1,065 | 0 s | L3 REPLAY_NONCE |
| Duplicate | 72 / 72 | 359 / 359 | 0 s | L3 DUPLICATE_ID |
| Missing fields | 82 / 82 | 483 / 483 | 0 s | L1 SCHEMA_MISSING |
| NaN / negative | 76 / 76 | 420 / 420 | 0 s | L1 SCHEMA_RANGE, L4 |
| Inflated ASN | 10 / 10 | 10 / 10 | 0 s | L8 ASN_OUTLIER |
| Stale timestamp | 71 / 71 | 425 / 425 | 0 s | L3 STALE_TS |
| Blackout | 47 / 49 | 167 / 248 silenced sources | 40 s | L9 SLA_SILENT |
| Cascade failure | 8 / 19 | — (physical event) | 3.5 h | L7 TWIN_ENVELOPE (divergence) |
| **All** | **536 / 561 (95.5 %)** | precision 0.959 · recall 0.707 · F1 0.814 | | FPR 0.19 % |

Cascade failures of DCs and suppliers publish no status of their own. They are visible only as missing flows (delivery departures, ASNs) or backlogs that diverge from the twin, and in this benchmark those flows are sparse, about 10 departures per DC per day. Port closures are caught at the next clean port report. All 6 twin divergences in the benchmark fell on attacked nodes (no false alarms).
