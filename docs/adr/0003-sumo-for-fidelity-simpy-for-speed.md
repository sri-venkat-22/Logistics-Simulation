# ADR-0003: SUMO for fidelity, SimPy for speed

- **Status:** Accepted (2026-09-30)
- **Context:** Street-level traffic (congestion, closures, rerouting) needs a microscopic simulator. Nation-wide what-ifs need hundreds of replications in seconds (NFR-3). No single engine does both.
- **Decision:** A two-scale co-simulation. SimPy macro twin nation-wide; SUMO micro twin (libsumo, in-process) for the Hyderabad belt only. Live mode couples both under one orchestrator clock: macro dispatches spawn SUMO trucks, SUMO arrivals fire macro receipts, and SUMO corridor travel-time distributions calibrate macro lane lead times. Monte Carlo runs macro-only using those calibrated distributions (~1000× faster).
- **Consequences:** Fidelity where it is visible, speed where it is needed. Time-sync becomes a risk (R-2), handled by a single clock owner and a handshake integration test.
