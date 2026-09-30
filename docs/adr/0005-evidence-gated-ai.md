# ADR-0005: Evidence-gated AI Copilot

- **Status:** Accepted (2026-09-30)
- **Context:** A natural-language Copilot makes what-ifs accessible, but an LLM that can mutate operational state is a safety and trust problem, and judges will ask "what if it hallucinates?"
- **Decision:** The Copilot (Claude with tool use) may read state, create and run scenarios, compare plans and explain KPIs. The tool `propose_apply(plan_id)` only returns a confirmation card, and applying requires a human `planner` click (`POST /plans/{id}/apply`). Tool outputs are treated as data. There is no SQL or shell tool. Scenario specs must validate against a strict JSON schema. Every claim links to evidence (event-log rows, seeds).
- **Consequences:** Safe by construction and easy to explain. The scripted demo question's responses are cached to remove latency risk (R-6).
