"""Validate every scenario template against the DSL and the network, and refresh the JSON Schema.

    python -m sim.scenarios.validate
"""
from __future__ import annotations

import sys

from sim.macro.network import Network
from sim.paths import SCENARIO_TEMPLATES
from sim.scenarios.dsl import Scenario, ScenarioType, write_json_schema


def main() -> int:
    net = Network()
    node_types = {n.id: n.type for n in net.nodes.values()}
    seen: set[ScenarioType] = set()
    for p in sorted(SCENARIO_TEMPLATES.glob("*.json")):
        sc = Scenario.load(p)
        sc.validate_against(node_types, net.lanes)
        seen.add(sc.type)
        print(f"  ok  {p.name:<22} {sc.type.value:<17} target={sc.target:<18} start={sc.start:<5} "
              f"{sc.duration_h:>6g} h  severity={sc.severity}  hash={sc.spec_hash()[:12]}")
    missing = set(ScenarioType) - seen
    if missing:
        print(f"  missing templates for: {sorted(m.value for m in missing)}")
        return 1
    write_json_schema(SCENARIO_TEMPLATES.parent / "scenario.schema.json")
    print("  wrote sim/scenarios/scenario.schema.json")
    return 0


if __name__ == "__main__":
    sys.exit(main())
