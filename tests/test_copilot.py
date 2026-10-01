"""Phase 7.4: the Copilot - strict tool schemas, the Claude tool loop (scripted client), the offline planner parser."""
import asyncio
from types import SimpleNamespace

from services.api.app import copilot as C


def test_tool_schemas_are_strict():
    defs = C.tool_definitions()
    assert {d["name"] for d in defs} == {"get_network_state", "find_at_risk_nodes", "create_scenario", "run_scenario",
                                         "optimize", "compare_plans", "explain_kpi", "propose_apply"}
    for d in defs:
        s = d["input_schema"]
        assert d["strict"] is True and s["additionalProperties"] is False and set(s["required"]) == set(s["properties"])
        assert "$defs" not in s and all("title" not in v for v in s["properties"].values())
    sc = next(d for d in defs if d["name"] == "create_scenario")["input_schema"]["properties"]
    assert "cyclone" in sc["template"]["enum"] and sc["target"]["type"] == ["string", "null"]


def test_what_if_parser():
    p = C.parse_what_if("What if a cyclone closes Chennai port for 5 days?")
    assert p["template"] == "cyclone" and p["target"] == "PORT_CHENNAI" and p["duration_h"] == 120
    p = C.parse_what_if("simulate a strike at Nagpur for 2 days")
    assert p["template"] == "strike" and p["target"] == "DC_NAGPUR" and p["duration_h"] == 48
    assert C.parse_what_if("hello there") is None
    p = C.parse_what_if("what if Patancheru plant has a 3 day outage? 20 runs over 10 days")
    assert (p["template"], p["target"], p["duration_h"], p["n"], p["days"]) == ("supplier_failure", "PLANT_PATANCHERU", 72, 20, 10)


class _Stream:
    def __init__(self, events, final):
        self.events, self.final = events, final

    async def __aenter__(self):
        return self

    async def __aexit__(self, *a):
        return False

    def __aiter__(self):
        async def gen():
            for e in self.events:
                yield e
        return gen()

    async def get_final_message(self):
        return self.final


class _Client:
    def __init__(self, script):
        self.script, self.calls = list(script), []
        self.beta = SimpleNamespace(messages=SimpleNamespace(stream=self._stream))

    def _stream(self, **kw):
        self.calls.append({**kw, "messages": list(kw["messages"])})
        return self.script.pop(0)


class _Tools:
    async def call(self, name, args):
        assert name == "get_network_state" and args == {}
        return {"twin_time": "2026-10-17T10:00"}, "twin 2026-10-17T10:00"


def test_claude_tool_loop_streams_text_runs_tools_and_stops():
    use = SimpleNamespace(type="tool_use", id="toolu_1", name="get_network_state", input={})
    first = _Stream([SimpleNamespace(type="text", text="Checking")],
                    SimpleNamespace(stop_reason="tool_use", content=[use], model=C.MODEL))
    second = _Stream([SimpleNamespace(type="text", text=" all good")],
                     SimpleNamespace(stop_reason="end_turn", content=[SimpleNamespace(type="text", text=" all good")], model=C.MODEL))
    client = _Client([first, second])

    async def run():
        return [e async for e in C.claude_turn(_Tools(), [{"role": "user", "content": "status?"}], client=client)]
    events = asyncio.run(run())
    kinds = [e["type"] for e in events]
    assert kinds == ["text", "tool_call", "tool_result", "text", "done"] and events[2]["ok"]
    kw = client.calls[0]
    assert kw["model"] == "claude-opus-5-5" and kw["fallbacks"] == "default" and kw["output_config"] == {"effort": "medium"}
    assert kw["cache_control"] == {"type": "ephemeral"} and all(t["strict"] for t in kw["tools"])
    followup = client.calls[1]["messages"]
    assert followup[-1]["role"] == "user" and followup[-1]["content"][0]["tool_use_id"] == "toolu_1"


def test_claude_tool_loop_rejects_invalid_input_and_handles_refusal():
    bad = SimpleNamespace(type="tool_use", id="toolu_2", name="find_at_risk_nodes", input={"limit": "lots"})
    first = _Stream([], SimpleNamespace(stop_reason="tool_use", content=[bad], model=C.MODEL))
    refused = _Stream([], SimpleNamespace(stop_reason="refusal", content=[], model=C.MODEL))
    client = _Client([first, refused])

    async def run():
        return [e async for e in C.claude_turn(C.Tools(None, None), [{"role": "user", "content": "x"}], client=client)]
    events = asyncio.run(run())
    res = next(e for e in events if e["type"] == "tool_result")
    assert res["ok"] is False and "INVALID" in res["summary"]
    assert client.calls[1]["messages"][-1]["content"][0]["is_error"] is True
    assert events[-1] == {"type": "done", "mode": "claude", "model": C.MODEL, "stop_reason": "refusal"}


def test_tool_results_turn_numpy_scalars_into_numbers():
    import numpy as np
    out = C._trim({"on_hand": np.float64(676.3), "n": np.int64(3), "arr": np.array([1.5, 2.0])})
    assert out == {"on_hand": 676.3, "n": 3, "arr": [1.5, 2.0]} and isinstance(out["on_hand"], float)
