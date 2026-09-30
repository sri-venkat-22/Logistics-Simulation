# ADR-0002: A Reality Emulator plays the physical world

- **Status:** Accepted (2026-09-30)
- **Context:** No real fleet, IoT devices or ERP exist. The twin still needs a "reality" to mirror, ground truth to score fidelity against, and a safe place to inject attacks.
- **Decision:** Run a second instance of the coupled SimPy + SUMO engines with a different seed and hidden perturbations (unannounced delays, demand drift). It publishes device-style telemetry (1 Hz GPS with noise, stock counts, ASNs, port status) to the public ingest API, exactly as real devices would. An attack injector adds labelled attacks. Real public feeds (Open-Meteo, AISStream, OSM, DataCo) are integrated alongside.
- **Consequences:** Every fidelity and detection metric has ground truth. The ingest path is identical for synthetic and real sources. We say this openly in the pitch (risk R-5).
