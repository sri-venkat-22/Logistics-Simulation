# ADR-0001: Modular monolith first, containers later

- **Status:** Accepted (2026-09-30)
- **Context:** Four people, ~24 days. Levels 2–3 reward a working end-to-end demo; Level 4 rewards Kubernetes, autoscaling and self-healing. Microservices on Day 1 would spend the critical path on plumbing (service discovery, contracts, local orchestration).
- **Decision:** Ship one FastAPI application with hard module boundaries (`ingest`, `trust`, `twin`, `scenarios`, `optimize`, `eval`, `copilot`, `auth`, `chaos`, `ws`). Modules talk only through Redis Streams or explicit service interfaces, never through each other's tables. In Phase 10, each module that needs independent scaling (`ingest`, `trust-worker`, `sim-worker`, `sumo-runner`) becomes its own container from the same codebase with a different entrypoint.
- **Consequences:** Fast iteration and one deploy until Level 3. Splitting is cheap because the stream boundaries already exist. Risk: boundary erosion. Mitigation: import-linter rules in CI.
