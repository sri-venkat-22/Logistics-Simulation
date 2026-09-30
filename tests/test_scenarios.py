"""Scenario DSL: templates, validation errors, hashing, timing."""
import json
from datetime import datetime, timedelta, timezone

import pytest
from pydantic import ValidationError

from sim.macro.network import Network
from sim.paths import SCENARIO_TEMPLATES
from sim.scenarios.dsl import Scenario, ScenarioType, point_in_polygon

BASE = {"type": "port_closure", "target": "PORT_CHENNAI", "polygon": None, "start": "+6h",
        "duration_h": 120, "severity": 1.0, "params": {}}


def test_one_template_per_type(net: Network):
    types = {}
    for p in SCENARIO_TEMPLATES.glob("*.json"):
        sc = Scenario.load(p)
        sc.validate_against({n.id: n.type for n in net.nodes.values()}, net.lanes)
        types[sc.type] = p.name
    assert set(types) == set(ScenarioType)


def test_defaults_and_typed_params():
    sc = Scenario.model_validate(BASE)
    assert sc.params == {"capacity_factor": 0.0, "divert_to": []}
    cy = Scenario.model_validate({**BASE, "type": "cyclone", "params": {"radius_km": 80}})
    assert cy.p.radius_km == 80 and cy.p.lane_time_multiplier == 2.0


@pytest.mark.parametrize("patch", [
    {"params": {"nonsense": 1}},             # unknown param for the type
    {"severity": 1.5},                       # out of range
    {"duration_h": 0},                       # must be > 0
    {"start": "tomorrow"},                   # bad time format
    {"polygon": [[80, 13], [81, 13]]},      # < 3 points
    {"polygon": [[200, 13], [81, 13], [81, 14]]},  # invalid lon
    {"type": "earthquake"},                  # unknown type
    {"extra_field": True},                   # extra keys forbidden
    {"target": "PORT CHENNAI; DROP"},        # target pattern
])
def test_rejects_invalid(patch):
    with pytest.raises(ValidationError):
        Scenario.model_validate({**BASE, **patch})


def test_target_kind_checked(net: Network):
    sc = Scenario.model_validate({**BASE, "target": "DC_NAGPUR"})
    with pytest.raises(ValueError, match="cannot target"):
        sc.validate_against({n.id: n.type for n in net.nodes.values()}, net.lanes)
    with pytest.raises(ValueError, match="unknown target"):
        Scenario.model_validate({**BASE, "target": "PORT_ATLANTIS"}).validate_against({}, [])


def test_start_offsets():
    t0 = datetime(2026, 10, 15, tzinfo=timezone(timedelta(hours=5, minutes=30)))
    assert Scenario.model_validate({**BASE, "start": "+6h"}).start_offset_h(t0) == 6
    assert Scenario.model_validate({**BASE, "start": "+2d"}).start_offset_h(t0) == 48
    assert Scenario.model_validate({**BASE, "start": "+90m"}).start_offset_h(t0) == 1.5
    assert Scenario.model_validate({**BASE, "start": "now"}).start_offset_h(t0) == 0
    assert Scenario.model_validate({**BASE, "start": "2026-10-16T00:00:00+05:30"}).start_offset_h(t0) == 24


def test_spec_hash_is_canonical():
    a = Scenario.model_validate({**BASE, "name": "A"})
    b = Scenario.model_validate(json.loads(json.dumps({**BASE, "name": "B"}, sort_keys=True)))
    assert a.spec_hash() == b.spec_hash()  # labels don't change the cache key
    assert a.spec_hash() != Scenario.model_validate({**BASE, "severity": 0.5}).spec_hash()


def test_polygon_closed_and_pip():
    sc = Scenario.load(SCENARIO_TEMPLATES / "cyclone.json")
    assert sc.polygon[0] == sc.polygon[-1]
    assert point_in_polygon(80.3, 13.1, sc.polygon)       # Chennai port
    assert not point_in_polygon(78.43, 17.24, sc.polygon)  # Hyderabad


def test_schema_file_current():
    on_disk = json.loads((SCENARIO_TEMPLATES.parent / "scenario.schema.json").read_text())
    assert on_disk["properties"].keys() == Scenario.model_json_schema()["properties"].keys()
