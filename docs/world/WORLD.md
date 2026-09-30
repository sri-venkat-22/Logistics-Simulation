# Phase 2 — World building: data and scenarios

**AEGIS Twin · Days 2–5 · critical path** · status: **done** (30 Sep 2026)

Exit criteria:

| Criterion | Command | Result |
|---|---|---|
| Macro twin prints KPIs | `python -m sim.macro.run --days 30` | ✅ 30 days simulated in ~0.06 s; fill rate, OTIF, backorders, cover, cost, CO₂, per-SKU and per-DC tables |
| Micro twin moves trucks across Hyderabad | `python -m sim.micro.run` | ✅ trucks drive real OSM roads (ORR, Medak Rd, Mumbai Hwy) at 100–130× real time |
| 500 trucks + 3,000 cars faster than real time (libsumo) | `python -m sim.micro.bench` | ✅ **71.6×** real time overall, **44.4×** in the busiest 5 min, peak **3,496** simultaneous vehicles, 0 teleports |
| 7 scenario templates, one per type, Pydantic-validated | `python -m sim.scenarios.validate` | ✅ all 7 validate against the DSL and the network |
| Tests | `python -m pytest` | ✅ 38 passed |

Set up with `python3 -m venv .venv && .venv/bin/pip install -r requirements.txt`. All commands below assume `.venv/bin/python`.

---

## 1. Network dataset (`data/nodes.json`, `data/lanes.json`)

Generated deterministically by [`data/generate_network.py`](../../data/generate_network.py). Coordinates are real site locations, accurate to about 1 km.

| Type | Nodes |
|---|---|
| Ports (5) | JNPT / Nhava Sheva, Mundra, Chennai, Visakhapatnam, Kolkata / Haldia |
| Plants / suppliers (5) | Patancheru pharma API plant (Hyderabad) → vaccines · Sriperumbudur auto & electronics cluster (Chennai) → electronics · Ahmedabad textiles & FMCG → FMCG · Chakan electronics (Pune) → electronics · **Shenzhen electronics (overseas, sea only)** |
| DCs (6) | Hyderabad-Medchal (FMCG/e-com) · Hyderabad-Shamshabad (pharma cold chain, near RGIA) · Bengaluru-Hoskote · Nagpur central hub · Delhi-NCR (Gurugram) · Pune-Chakan. Cold chain: Shamshabad, Bengaluru, Delhi, Pune. |
| Demand zones (12) | Hyderabad, Bengaluru, Chennai, Mumbai, Delhi, Kolkata, Pune, Ahmedabad, Vijayawada, Visakhapatnam, Coimbatore, Jaipur (population-weighted) |

**Lanes (49).** Lane IDs L001–L042 are unchanged from Level 1, so the prototype and scenario references stay valid. L043–L049 were added so that every zone has a cold-chain DC and FMCG reaches the south.

| Mode | Lanes | Distance | Distance source | ₹ / unit-km | CO₂ kg / t-km | Lead time (mean) | σ (log) | Capacity / day |
|---|---|---|---|---|---|---|---|---|
| Road | 31 | 16–1,482 km | **OSRM** driving distance (cached in `data/osrm_lanes.json`) | 0.060 | 0.062 | 4.4–43 h | 0.25 | 6,000 |
| Rail | 12 | 661–1,607 km | haversine × 1.3 detour | 0.030 | 0.022 | 42–75 h | 0.35 | 15,000 |
| Sea | 4 | 5,579–7,209 km | great-circle legs via real shipping waypoints (S. China Sea → Singapore → Malacca → Bay of Bengal; west-coast ports also round Sri Lanka) | 0.008 | 0.012 | 12–15 days | 0.30 | 40,000 |
| Air | 2 | 492–1,283 km | haversine × 1.05 | 0.450 | 0.600 | 7–8 h | 0.15 | 800 |

Lead time is lognormal: mean = distance / effective speed + fixed handling (road 38 km/h + 4 h, rail 28 km/h + 18 h, sea 26 km/h + 72 h, air 650 km/h + 6 h). Each lane stores `lt_mu`, `lt_sigma`, `lt_mean_h`, `lt_p50_h` and `lt_p90_h`, so E[T] = exp(μ + σ²/2) holds exactly (tested).

**Sourcing (`data/sourcing.json`).** For each DC × SKU there is a primary path plus up to 3 alternates (ready for rerouting in Phase 7), and each zone × SKU has a serving DC with backups. The rules:
- vaccines only pass through cold-chain DCs;
- electronics prefer the **sea-imported** Shenzhen source;
- everything else takes the fastest path.

## 2. SKUs (`data/skus.json`)

| SKU | Family | Unit | Value | Weight | Traits | Base demand |
|---|---|---|---|---|---|---|
| `SKU_VAX` | vaccine | box of 100 doses | ₹1,850 | 2.5 kg | perishable (720 h), **cold chain**, high value | 9 / million people / day |
| `SKU_FMCG` | fmcg | case | ₹120 | 9 kg | **high volume** | 140 / million / day |
| `SKU_ELEC` | electronics | carton | ₹9,400 | 6 kg | high value, **sea-imported** | 12 / million / day |

## 3. Demand model

```
mean(zone, sku, day) = pop_m(zone) × base(sku) × weekday[family] × festival(family, date) × scenario
D ~ NegativeBinomial(mean, k)   (gamma–Poisson; Var = mean + mean²/k)
```

**Fitted from DataCo** by [`data/demand/fit_dataco.py`](../../data/demand/fit_dataco.py) → [`demand_params.json`](../../data/demand/demand_params.json).
- Source: *DataCo Smart Supply Chain for Big Data Analysis*, Mendeley Data v5, doi:10.17632/8gx2fvg2k6.5, CC BY 4.0; 180,519 order lines. The raw CSV is git-ignored; the fitted parameters are committed.
- Rows used: 164,572, excluding cancelled and fraud orders.

| Family | DataCo proxy | Window | Active days | Weekday profile (Mon…Sun) | Over-dispersion k | Daily CV |
|---|---|---|---|---|---|---|
| electronics | Technology dept. + electronics categories | 2015-01 → 2017-09 | 814 | 1.00 0.99 0.97 0.95 1.02 1.02 1.04 | **3.85** | 0.59 |
| fmcg | all other departments | 2015-01 → 2017-09 | 1,004 | 0.99 1.00 0.99 1.00 1.01 1.00 1.01 | **107.5** | 0.11 |
| vaccine | Health & Beauty (closest health proxy) | 2017-11 → 2018-01 | 8 | ≈ global (shrink w = 0.04) | 107.5 (borrowed; own estimate unreliable) | — |

**Caveats (stated, not hidden):**
- **Proxy data.** DataCo is US-style retail with no pharma volume, so the vaccine family is a weak proxy.
- **Window.** Order volume and product mix change after Sep 2017, so the stable window ends there. Proxies that only exist later (Technology, Health & Beauty) are fitted on their own date range.
- **Seasonality.** DataCo's weekly and monthly profiles are nearly flat (±5%). India's seasonality therefore comes mainly from the **festival calendar** ([`festivals.json`](../../data/demand/festivals.json)): Diwali adds **+60%** for FMCG and electronics (none for vaccines). The effect ramps up over 12 days, holds a 3-day plateau and decays over 2 days. The calendar has Diwali dates for 2025, 2026 (8 Nov) and 2027.
- **Lead-time cross-check.** DataCo's realised / scheduled shipping-time ratio has a log-σ of about 0.39 (Standard Class), higher than our road prior of 0.25. Phase 3 should recalibrate road σ from SUMO corridor times and this ratio.

## 4. SUMO Hyderabad micro-twin (`sim/micro/hyderabad/`)

Built by `python -m sim.micro.build_hyderabad`:

1. **OSM source**
   - **bbox** 78.22,17.15 → 78.53,17.66 (the western and southern belt: Medchal – ORR – Gachibowli – Shamshabad – Patancheru), not the whole ORR.
   - **Road types** motorway → tertiary (+ links) only.
   - **Why not Overpass:** `osmGet.py` against Overpass (the plan's route) returned **504 Gateway Timeout** on every public mirror for the dense eastern strip. The build therefore uses the **OpenStreetMap France Telangana extract**: snapshot of 29 Sep 2026, 104 MB, md5-verified, clipped and filtered locally with pyosmium. That gives one consistent snapshot, and `--download --overpass` remains as the fallback.
   - **Clip result:** 10,327 ways and 86,034 nodes kept.
2. **netconvert**
   - Filters: `--keep-edges.by-type highway.motorway…tertiary_link --geometry.remove --ramps.guess --junctions.join`.
   - Also: TLS guessing and joining, roundabout guessing, bbox trim, and largest connected component only.
   - **Result: 10,670 edges, 5,690 junctions, 6,742 lane-km** (`hyderabad.net.xml.gz`).
3. **Vehicle types:** `truck` (vClass truck, maxSpeed 22 m/s, length 12 m) and `car` (passenger).
4. **Hubs as parkingAreas**, each snapped to the nearest truck-accessible edge ≥ 60 m long (`hubs.json`, `parking.add.xml`):

| Hub | Kind | Lat, lon | Edge | Street | Snap |
|---|---|---|---|---|---|
| Medchal DC | dc | 17.630, 78.480 | `810375272` | — | 69 m |
| Shamshabad DC | dc | 17.240, 78.430 | `29113344#1` | Terminal 1 Approach Rd | 90 m |
| Patancheru Plant | plant | 17.530, 78.260 | `573890896#2` | — | 17 m |
| Kothur Pharma | plant | 17.160, 78.290 | `432322837` | — | 498 m |
| Jeedimetla Industrial | industrial | 17.515, 78.455 | `-354041114#1` | — | 202 m |
| Kompally | consumer | 17.536, 78.487 | `1460401723` | Nizamabad Road | 254 m |
| Kukatpally | consumer | 17.494, 78.399 | `673625382#1` | Nizampet – JNTU Rd / Mumbai Hwy | 100 m |
| Gachibowli | consumer | 17.440, 78.348 | `211221856#5` | ISB Road | 82 m |
| Hitec City | consumer | 17.447, 78.376 | `89914125#1` | Deloitte driveway | 52 m |
| Mehdipatnam | consumer | 17.395, 78.437 | `375638015#1` | P.V. Narasimha Rao Flyover | 1 m |
| Secunderabad | consumer | 17.439, 78.498 | `-1201252790` | Market road | 70 m |
| RGIA Air Cargo | airport | 17.232, 78.448 | `-1240151613` | — | 645 m |

5. **Background traffic:** `randomTrips.py` with fringe factor 5 and trips of at least 3 km produces 7,200 validated car routes over 1 hour (`cars.rou.xml`). The config also includes 60 demo trucks that park at their destination hub.
6. **Road flood:** the `road_flood` template's `sumo_edges` are filled automatically with the 2 ORR edges (both carriageways) near 17.283°N 78.383°E, between Shamshabad and Gachibowli.

**Runner API** (`sim/micro/runner.py`, libsumo in-process; TraCI + sumo-gui with `--gui`):
- `spawn_truck`, `close_road` / `reopen_road` (lanes disallowed plus a travel-time penalty, then `rerouteTraveltime`);
- `reroute_all`, `step` (collects arrivals);
- `positions` (id, lon, lat, speed, angle, edge);
- `edge_travel_times` (for calibration).

The Phase 3 coupling orchestrator drives the micro-twin through this API.

**Benchmark** (`python -m sim.micro.bench`, results in [`benchmark.json`](../../sim/micro/hyderabad/benchmark.json)). All vehicles depart within the first 5 minutes, so they share the network:

| Trucks | Cars | Peak simultaneous | Sim time | Wall time | Real-time factor | Busiest 5 min | Teleports |
|---|---|---|---|---|---|---|---|
| 500 | 3,000 | **3,496** | 30 min | 25.1 s | **71.6×** | **44.4×** (3,449 vehicles avg) | 0 |

Machine: Apple silicon (arm64), macOS, Python 3.11.9, SUMO 1.27.1, single process.

## 5. Scenario DSL (`sim/scenarios/dsl.py`)

A Pydantic v2 model (`extra="forbid"`) with typed params per scenario type. JSON Schema for the Copilot is at [`scenario.schema.json`](../../sim/scenarios/scenario.schema.json).

```json
{ "type": "port_closure | cyclone | road_flood | demand_spike | supplier_failure | strike | data_blackout",
  "target": "PORT_CHENNAI", "polygon": null, "start": "+6h", "duration_h": 120, "severity": 1.0, "params": {} }
```

- **`target`**: a node id, lane id or `*`. Each type allows specific kinds (for example, `port_closure` → port only) via `validate_against(network)`.
- **`polygon`**: a `[lon, lat]` ring; it is validated and closed automatically.
- **`start`**: `now`, `+90m`, `+6h`, `+2d`, or an ISO-8601 timestamp.
- **`severity` (0–1)** interpolates linearly between no effect and the full params effect.
- **`spec_hash()`** is SHA-256 of the canonical JSON, with labels excluded. It is the Phase 4 cache key.

| Template | Type | Target | Start / duration | Severity | Params |
|---|---|---|---|---|---|
| `port_closure.json` | port_closure | PORT_CHENNAI | +6 h / 120 h | 1.0 | capacity 0, divert → Vizag |
| `cyclone.json` | cyclone | PORT_CHENNAI + track polygon | +6 h / 120 h | 0.9 | lanes × 2.5, close nodes inside |
| `road_flood.json` | road_flood | DC_HYD_SHAMSHABAD | +2 h / 36 h | 0.8 | lanes L006, L015, L047; 2 SUMO ORR edges; speed 0.15 |
| `demand_spike.json` | demand_spike | Z_HYD | +0 h / 168 h | 1.0 | × 1.8 FMCG + electronics |
| `supplier_failure.json` | supplier_failure | PLANT_PATANCHERU | +12 h / 72 h | 1.0 | capacity 0 |
| `strike.json` | strike | DC_NAGPUR | +1 d / 48 h | 0.7 | throughput 0.3 |
| `data_blackout.json` | data_blackout | `*` | +1 h / 10 min | 1.0 | 30% of sources (trust layer; no physical effect) |

## 6. Macro twin v0 (`sim/macro/`)

`engine.py` is a SimPy model whose clock runs in hours:
- **Demand:** one order per zone × SKU per day, sent to its serving DC.
- **Fulfilment:** stock is allocated at once; shortfalls are backordered FIFO and filled when stock arrives.
- **Dispatch:** each DC has a daily throughput budget, which strikes reduce.
- **Replenishment:** a dynamic **(s, S)** policy is reviewed daily at 06:00 against the *forecast*. Known festivals are therefore pre-built; unannounced shocks are not.
- **Policy per family:** vaccines z = 2.05 with 2 days' cover (cold-chain space is scarce); FMCG z = 1.65 with 5 days; electronics z = 1.65 with 10 days (batched sea imports).
- **Production:** plants split output across open orders and ship daily at 18:00.
- **Transit:** multi-leg lognormal shipments. Closed nodes hold cargo at anchorage or in the yard. A lane slowdown only affects the part of a transit that overlaps the disruption.
- **Initial state:** the model starts in **steady state**, with a pipeline already in transit.
- **Random numbers:** streams are keyed by name, so baseline and scenario runs share **common random numbers**.

30 days from 15 Oct 2026 (the window includes Diwali), seed 42. Each template was run alone, plus three stress tests longer than the buffers:

| Run | Fill rate | OTIF | Vaccine fill | FMCG fill | Electronics fill | Backorders (end) | Total cost | CO₂ |
|---|---|---|---|---|---|---|---|---|
| baseline | 100.0% | 99.6% | 100.0% | 100.0% | 100.0% | 0 | ₹551.5 L | 337.3 t |
| cyclone | 100.0% | 99.6% | 100.0% | 100.0% | 100.0% | 0 | ₹551.1 L (−0.3) | 337.3 t |
| port_closure | 100.0% | 99.6% | 100.0% | 100.0% | 100.0% | 0 | ₹551.1 L (−0.3) | 337.3 t |
| road_flood | 100.0% | 99.6% | 100.0% | 100.0% | 100.0% | 0 | ₹551.5 L | 337.3 t |
| demand_spike | 100.0% | 99.6% | 100.0% | 100.0% | 100.0% | 0 | ₹563.2 L (+11.7) | 347.5 t |
| supplier_failure | 100.0% | 99.6% | 100.0% | 100.0% | 100.0% | 0 | ₹551.4 L | 337.3 t |
| strike | 100.0% | 99.6% | 100.0% | 100.0% | 100.0% | 0 | ₹551.3 L | 337.3 t |
| data_blackout | 100.0% | 99.6% | 100.0% | 100.0% | 100.0% | 0 | ₹551.5 L | 337.3 t |
| stress: Patancheru outage 7 d | 99.3% | 94.9% | **86.2%** | 100.0% | 100.0% | 0 | ₹595.4 L (+44.0) | 337.2 t |
| stress: Ahmedabad outage 10 d | **60.3%** | 86.5% | 100.0% | **54.6%** | 100.0% | 0 | ₹656.8 L (+105.3) | 327.2 t |
| stress: Chennai port closed 21 d | 100.0% | 99.2% | 100.0% | 100.0% | 99.7% | 0 | ₹549.8 L (−1.7) | 337.3 t |

Stock-out hours: Patancheru 7 d → vaccines at Delhi 106 h, Pune 87 h, BLR 84 h. Ahmedabad 10 d → FMCG at Delhi 450 h, Pune 237 h, BLR 155 h. Chennai 21 d → electronics at Shamshabad 82 h. Transport cost is booked on delivery, so cargo held past the horizon *lowers* cost inside the window. Phase 3 should book cost at dispatch or report an in-transit value.

## 7. Findings for the team (decisions needed)

1. **At template severity, the demo story's shock is absorbed.** With realistic sourcing, Hyderabad's vaccines come from Patancheru (and in reality from Genome Valley), not through Chennai. A 5-day Chennai cyclone therefore delays electronics receipts until the port reopens (tested) but causes no stock-out: DCs carry about 18 days of electronics cover. The Level-1 mock line "Shamshabad stocks out in 3.2 days" is **not** reproduced by the twin. Options:
   - (a) retell the demo around a **Patancheru outage** (the lean cold chain holds ≈ 3 days of vaccine cover on average, so a 7-day outage breaks it) or the **Ahmedabad single-source** risk;
   - (b) add an imported-vaccine inbound via Chennai port to the network (a data decision, not a code change);
   - (c) show a **combined** scenario: cyclone plus the Diwali peak.
2. **FMCG is single-sourced.** Every DC's FMCG comes from Ahmedabad; a 10-day outage cuts FMCG fill to 55%. This is the strongest single point of failure for the Network Graph and REI story, and a natural target for the Phase 7 optimiser (dual sourcing).
3. **Overpass is not reliable enough to depend on.** Keep the extract-based build, and commit the built net (`hyderabad.net.xml.gz`, 6 MB) so nobody needs to download OSM data on demo day.

## 8. Reproduce

```bash
python3 -m venv .venv && .venv/bin/pip install -r requirements.txt
```

```bash
.venv/bin/python data/generate_network.py && .venv/bin/python data/demand/fit_dataco.py
```

```bash
.venv/bin/python -m sim.micro.build_hyderabad --download
```

```bash
.venv/bin/python -m sim.macro.run --days 30 --scenario cyclone --compare
```

```bash
.venv/bin/python -m sim.micro.run --minutes 30 --trucks 60 --flood
```

```bash
.venv/bin/python -m sim.micro.bench --out sim/micro/hyderabad/benchmark.json
```

```bash
.venv/bin/python -m pytest
```

Notes on the commands above:
- `fit_dataco.py` needs `data/dataco/DataCoSupplyChainDataset.csv`, downloaded from the Mendeley record above.
- Omit `--download` from the micro-twin build if the Telangana extract is already cached in `sim/micro/hyderabad/osm/`.
