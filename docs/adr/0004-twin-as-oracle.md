# ADR-0004: The twin validates the data (twin-as-oracle)

- **Status:** Accepted (2026-09-30)
- **Context:** Physics checks catch crude spoofs (teleports), and a per-vehicle Kalman filter catches drift. A coordinated spoof that is physically plausible (smooth, on-road, right speed) passes both.
- **Decision:** Trust layer 7 compares every reported position with the twin's own predicted position on the vehicle's planned route, and flags reports outside the P99 envelope. Accepted data updates the twin; rejected data goes to quarantine and the entity keeps its estimate.
- **Consequences:** Makes detection a feature of having a twin, which is a novelty claim for the pitch. The feedback loop must be guarded: the twin's prediction never overrides consistent multi-source evidence. Layer 9 reputation and cross-source reconciliation break ties.
