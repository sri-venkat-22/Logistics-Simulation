# Trust layer — the 9-layer pipeline (Phase 7.5)

Every message passes the layers in order. The first verdict quarantines it with a reason code, so it never reaches the twin. L7 divergences of observed stock, port and flow state from the live twin are the exception: they are flagged and alerted but not quarantined, because reality may genuinely have moved.

| # | Layer | Where | Technique | Reason codes |
|---|---|---|---|---|
| L1 | Schema | `ingest.py` (gateway) | Pydantic v2 strict envelope + typed payload, finite numbers, ranges, kind ↔ channel | `SCHEMA_MISSING / RANGE / TYPE / EXTRA / KIND / ENVELOPE` |
| L2 | Authenticity | `pipeline.py` | Per-device HMAC-SHA256: the derived key HMAC(master, source), or a **rotated key** from `device_keys` (pgcrypto-encrypted; the previous key is honoured during a grace period). Publisher batches carry their own HMAC + nonce + timestamp window | `HMAC_INVALID` |
| L3 | Temporal | `pipeline.py` | Message-id dedupe (Redis `SET NX`, 1 h). A re-sent id behind the source's own clock is a **replay**, one in step with it a **duplicate**. Stale: > 60 s behind the source or > 1 h behind the world clock | `REPLAY_NONCE`, `DUPLICATE_ID`, `STALE_TS` |
| L4 | Physics | `pipeline.py` | Implied speed ≤ 200 km/h since the last accepted fix. The track re-anchors after 10 self-consistent fixes | `PHYSICS_TELEPORT` |
| L5 | Map-matching | `trust_layers.RoadIndex` | City fixes within ~110–220 m of a SUMO road edge: a 0.001° grid of 20 m samples of all 10,670 edges (`sim/micro/hyderabad/road_cells.npz`). National fixes on a road corridor: lane chords + city gateways, 0.05° grid | `OFFROAD` |
| L6 | State estimation | `trust_layers.KalmanGate` | Dead-reckoning Kalman filter per vehicle (below) | `KALMAN_GATE` |
| L7 | Twin oracle | `trust_layers.TwinOracle`, `TrustEngine` | (a) a shipment's fixes must stay in its planned route corridor (lanes learned from its ASN); (b) divergence of observed backlog, port status and flows (delivery departures, ASNs) from the live twin | `TWIN_ENVELOPE` |
| L8 | Feed anomaly | `trust_layers.FeedAnomaly` | ASNs: more than a truck-load on one note, upper-tail MAD z-score, ETA before ship time, IsolationForest. Stock: a count that rises by more than the ASNs due + 1.5 × the order-up-to level | `ASN_OUTLIER`, `RECON_MISMATCH` |
| L9 | Reputation + SLA | `trust_layers.Reputation`, `pipeline.SlaMonitor` | Beta reputation, time-decayed (τ = 30 min). Only content violations count, at most one per minute. Below 0.4 with ≥ 5 penalties the source is quarantined until clean messages lift it. Per-kind SLAs: GPS 30 s, stock 30 min, port 2 h → silent sources, `PREDICTED` vehicles, blackout alerts | `LOW_REPUTATION`, `SLA_SILENT` |

API: `GET /trust/layers` (live counts, divergences, lowest reputation), `GET /trust/benchmark`, `GET /trust/quarantine`, `GET /trust/stats`. The live Trust Center screen shows all of them.

## L6: the Kalman gate, and how it was tuned on the labelled data

A drift spoof moves the reported position at 0.5–3 m/s while the device's speed and heading stay honest. The residual between observed motion and dead reckoning from speed and heading is the signal. But honest national trucks carry a steady residual of about 1 m/s themselves: they are interpolated along the straight line while their odometer reports road speed. So the size of the residual can't separate an attack from honest motion; only its **onset** can.

- **Filter.** Per axis, a 2-state Kalman filter over [position, velocity bias], driven by trapezoidal dead reckoning. The bias is learned during a 60 s warm-up and then held almost constant (q = 10⁻⁶), so a drift that *starts* mid-segment accumulates in the innovation. When the Mahalanobis distance of that innovation fails the χ² gate (18.4, 2 dof, p = 0.9999) twice in a row, the fix is flagged.
- **Roll-back.** On a verdict the filter rolls back to its fused state from 5 minutes earlier, before the drift leaked into the bias, and coasts on dead reckoning. Honest fixes that match the coasted track are accepted again the moment a drift ends.
- **Bias plausibility.** A drift that begins exactly when a track (re)starts is absorbed into the bias. Honest bias has a known shape: along the reported heading a truck can only be slower than its odometer, and hardly sideways. The limits sit outside the 99.9th percentile of clean benchmark traffic: along ≤ +4 %, cross-track ≤ 8 % of speed nationally, ≤ 15 % in the city.
- **Coasting cap.** Coasting is capped at 120 s. After an undetected slow drift the snap-back would otherwise be flagged for a long time.

Each step was measured on the benchmark (the first two rows through the full pipeline, the rest by replaying the GPS stream through L6 alone):

| Version | Drift attacks caught | False positives |
|---|---|---|
| Position-only KF | 0 / 72 | 1,893 |
| + bias state, warm-up | 54 / 72 | 3,604 (all after attacks) |
| + roll-back | 53 / 72 | 2,721 |
| + bias plausibility, re-anchor on a known-bad track | 60 / 72 | 1,184 |
| + 120 s coasting cap (shipped) | **60 / 72** | **365** |

Zero false positives land on vehicles that were never attacked. The remaining 365 are the first fixes after a slow (0.5–0.7 m/s) drift that went undetected, when the position snaps back.

## Benchmark

```bash
.venv/bin/python -m services.api.app.trust_bench
```

The labelled red-team benchmark ([`data/benchmark/`](../../data/benchmark)) has 218,770 messages and 561 attacks across 10 types. It is replayed through exactly the live code path: `ingest.validate`, then `TrustStage.check` with an in-memory seen-set, the SLA sweep every 10 world-seconds, and a live twin advancing with the world clock. Results are in [`benchmark.json`](benchmark.json) and are deterministic (two runs give identical results).

| | Value |
|---|---|
| Attacks detected | **536 / 561 (95.5 %)** |
| Messages: precision / recall / F1 | 0.959 / 0.707 / 0.814 |
| False-positive rate on clean messages | 0.19 % (389 of 205,931) |
| Throughput (one core, all layers) | ≈ 46,000 msgs/s |
| Live check: 3 h of Emulator traffic through the API (SUMO city trucks at 1 Hz included) | 102,574 messages, **0 quarantined** |

Per attack type: [`docs/intelligence/PHASE7.md` § 7.5](../intelligence/PHASE7.md#75-trust-layer--all-9-layers).
