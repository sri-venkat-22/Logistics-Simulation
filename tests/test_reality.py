"""Phase 3d: Reality Emulator telemetry, hidden perturbations, attack injector ground truth, HTTP publishing."""
import hashlib
import hmac
import json
import math
import threading
from collections import Counter
from datetime import datetime, timedelta
from http.server import BaseHTTPRequestHandler, HTTPServer
from zoneinfo import ZoneInfo

import pytest
from pydantic import ValidationError

from sim.micro.process import MicroProcess
from sim.micro.runner import CFG
from sim.paths import DATA
from sim.reality.attacks import ATTACK_TYPES, EXPECTED, Attack, AttackInjector
from sim.reality.emulator import RealityEmulator
from sim.reality.schemas import Envelope
from sim.reality.telemetry import DEFAULT_MASTER, HttpSink, MemorySink, Signer
from sim.scenarios.dsl import haversine_km

START = datetime(2026, 10, 15, tzinfo=ZoneInfo("Asia/Kolkata"))
T0 = 82.0  # publish from day 4 10:00 (after warm-up); the window holds a supplier dispatch


@pytest.fixture(scope="module")
def run():
    """2 simulated hours of telemetry with every attack type, national trucks every 5 s."""
    inj = AttackInjector.random_campaign(80, 2.0, seed=11, t0_h=T0,
                                         mix={t: 1.0 for t in ATTACK_TYPES} | {"inflated_asn": 0.3, "cascade_failure": 0.5})
    inj.attacks.append(Attack("inflated_asn", T0 + 0.1, params={"factor": 10.0}, id="A-ASN"))
    sink = MemorySink()
    em = RealityEmulator(START, seed=1042, sink=sink, attacks=inj, national_gps_period_s=5.0, telemetry_from_h=T0)
    em.run_until(T0 + 2.0)
    em.close()
    return em, sink.messages, em.truth()


def test_messages_signed_and_schema_valid(run):
    em, msgs, truth = run
    assert Counter(m["kind"] for m in msgs).keys() >= {"gps", "stock", "port"}
    signer = Signer(DEFAULT_MASTER)
    labelled = {(x["msg_id"]) for a in truth["attacks"] for x in a["msgs"] if a["type"] in ("missing_fields", "nan_negative")}
    invalid = 0
    for m in msgs:
        assert signer.verify(m)  # compromised devices still sign: detection must be content-based
        try:
            Envelope.model_validate(m).typed_payload()
        except (ValidationError, ValueError):
            invalid += 1
            assert m["msg_id"] in labelled, m  # every malformed message is a labelled attack
    assert invalid > 0
    attacked = {x["msg_id"] for a in truth["attacks"] for x in a["msgs"]}
    per_src: dict[str, list] = {}
    for m in msgs:
        if m["msg_id"] not in attacked:
            per_src.setdefault(m["source_id"], []).append((datetime.fromisoformat(m["ts"]), m["seq"]))
    for rows in per_src.values():  # honest devices: timestamps and sequence numbers never go backwards
        assert rows == sorted(rows)
        assert rows[0][0] >= datetime.fromisoformat(em.ts(T0))


def test_every_attack_type_labelled(run):
    _, msgs, truth = run
    done = [a for a in truth["attacks"] if a["status"] == "done"]
    assert {a["type"] for a in done} == set(ATTACK_TYPES)
    for a in truth["attacks"]:
        assert (a["expected_layer"], a["expected_reason"]) == EXPECTED[a["type"]]
        if a["status"] == "done" and a["type"] not in ("cascade_failure",):
            assert a["msgs"], a
    ids = Counter(m["msg_id"] for m in msgs)
    for a in done:
        for x in a["msgs"]:
            if x["action"] == "dropped":
                assert x["msg_id"] not in ids  # withheld messages never reach the stream
            else:
                assert ids[x["msg_id"]] >= x["occurrence"] >= 1


def test_attack_semantics(run):
    em, msgs, truth = run
    by_id = {}
    for m in msgs:
        by_id.setdefault(m["msg_id"], []).append(m)
    att = {a["type"]: a for a in truth["attacks"] if a["status"] == "done"}
    tp = att["gps_teleport"]
    m = by_id[tp["msgs"][0]["msg_id"]][0]
    others = [x for x in msgs if x["source_id"] == tp["target"] and x["msg_id"] not in {y["msg_id"] for y in tp["msgs"]}]
    near = min(others, key=lambda x: abs(datetime.fromisoformat(x["ts"]) - datetime.fromisoformat(m["ts"])))
    assert haversine_km(m["payload"]["lat"], m["payload"]["lon"], near["payload"]["lat"], near["payload"]["lon"]) > 250
    for kind in ("replay", "duplicate"):
        x = att[kind]["msgs"][0]
        assert x["occurrence"] >= 2 and len(by_id[x["msg_id"]]) >= 2
        assert by_id[x["msg_id"]][0] == by_id[x["msg_id"]][1]  # byte-identical copy
    st = by_id[att["stale_timestamp"]["msgs"][0]["msg_id"]][0]
    assert datetime.fromisoformat(st["ts"]) < datetime.fromisoformat(em.ts(T0)) - timedelta(hours=1.5)
    asn = next(a for a in truth["attacks"] if a["attack_id"] == "A-ASN")
    inflated = by_id[asn["msgs"][0]["msg_id"]][0]
    honest = [m for m in msgs if m["kind"] == "asn" and m["msg_id"] != inflated["msg_id"]
              and m["payload"]["dc"] == inflated["payload"]["dc"] and m["payload"]["sku"] == inflated["payload"]["sku"]]
    assert inflated["kind"] == "asn" and honest
    assert inflated["payload"]["qty"] >= 9 * max(m["payload"]["qty"] for m in honest)
    nan = [by_id[x["msg_id"]][0] for x in att["nan_negative"]["msgs"]]
    assert any(any(isinstance(v, float) and (math.isnan(v) or math.isinf(v) or v < 0) for v in n["payload"].values()) for n in nan)
    bo = att["blackout"]
    assert bo["params"]["silenced"] and all(x["action"] == "dropped" for x in bo["msgs"])
    cf = att["cascade_failure"]
    assert cf["events"] and cf["events"][0]["node"] == cf["target"]


def test_hidden_perturbations_recorded(run):
    em, _, truth = run
    kinds = Counter(p["kind"] for p in truth["perturbations"])
    assert kinds["lane_bias"] == sum(1 for l in em.twin.net.lanes.values() if l.mode == "road")
    assert kinds["demand_drift"] >= 3
    assert all(1.0 <= p["multiplier"] <= 1.15 for p in truth["perturbations"] if p["kind"] == "lane_bias")


def test_reproducible_from_seeds():
    def once():
        sink = MemorySink()
        inj = AttackInjector.random_campaign(10, 0.5, seed=5, t0_h=T0)
        em = RealityEmulator(START, seed=77, sink=sink, attacks=inj, national_gps_period_s=10.0, telemetry_from_h=T0)
        em.run_until(T0 + 0.5)
        em.close()
        return sink.messages, em.truth()["attacks"]
    a, b = once(), once()
    assert json.dumps(a, sort_keys=True) == json.dumps(b, sort_keys=True)


def test_city_trucks_ping_at_1hz():
    if not CFG.exists():
        pytest.skip("micro-twin not built")
    sink = MemorySink()
    with MicroProcess(seed=1042, background=False, fcd_every=1) as mp:
        em = RealityEmulator(START, seed=1042, micro=mp, sink=sink, national_gps=False, telemetry_from_h=10.0)
        em.run_until(14.0)
        em.close()
    city = [m for m in sink.messages if m["kind"] == "gps" and m["payload"]["scope"] == "city"]
    assert city
    by_src = Counter(m["source_id"] for m in city)
    src = by_src.most_common(1)[0][0]
    ts = [datetime.fromisoformat(m["ts"]) for m in city if m["source_id"] == src]
    gaps = {round((b - a).total_seconds()) for a, b in zip(ts, ts[1:])}
    assert gaps == {1}  # one ping per simulated second
    w, s, e, n = 78.22, 17.15, 78.53, 17.66
    assert all(w - 0.05 < m["payload"]["lon"] < e + 0.05 and s - 0.05 < m["payload"]["lat"] < n + 0.05 for m in city)


class _Ingest(BaseHTTPRequestHandler):
    got: list = []

    def do_POST(self):  # noqa: N802
        body = self.rfile.read(int(self.headers["Content-Length"]))
        want = hmac.new(DEFAULT_MASTER, f"{self.headers['X-AEGIS-Timestamp']}\n{self.headers['X-AEGIS-Nonce']}\n".encode() + body,
                        hashlib.sha256).hexdigest()
        ok = hmac.compare_digest(want, self.headers["X-AEGIS-Signature"])
        _Ingest.got.append((self.path, ok, len(json.loads(body)["messages"])))
        self.send_response(202 if ok else 401)
        self.end_headers()

    def log_message(self, *a):
        pass


def test_http_sink_posts_signed_batches():
    srv = HTTPServer(("127.0.0.1", 0), _Ingest)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    sink = HttpSink(f"http://127.0.0.1:{srv.server_port}", batch_size=200)
    em = RealityEmulator(START, seed=3, sink=sink, national_gps_period_s=10.0, telemetry_from_h=60.0)
    em.run_until(60.25)
    em.close()
    srv.shutdown()
    paths = {p for p, _, _ in _Ingest.got}
    assert {"/api/v1/ingest/telemetry", "/api/v1/ingest/inventory"} <= paths
    assert all(ok for _, ok, _ in _Ingest.got) and sink.failed == 0
    assert sink.sent == sum(n for _, _, n in _Ingest.got) == sum(em.counts["by_kind"].values())
    assert max(n for _, _, n in _Ingest.got) <= 200


def test_committed_benchmark_manifest():
    m = json.loads((DATA / "benchmark" / "manifest.json").read_text())
    assert m["attacks"]["done"] >= 500 and set(m["attacks"]["by_type"]) == set(ATTACK_TYPES)
    assert all(v["done"] > 0 for v in m["attacks"]["by_type"].values())
    assert m["messages"]["malicious"] > 0 and m["messages"]["clean"] > 10 * m["messages"]["malicious"]
    labels = [json.loads(l) for l in (DATA / "benchmark" / "labels.jsonl").read_text().splitlines()]
    assert hashlib.sha256((DATA / "benchmark" / "labels.jsonl").read_bytes()).hexdigest() == m["files"]["labels.jsonl"]
    assert len(labels) == m["attacks"]["scheduled"]


@pytest.mark.slow
def test_benchmark_regenerates_identically(tmp_path):
    from sim.reality.bench import build
    m = build(tmp_path)
    ref = json.loads((DATA / "benchmark" / "manifest.json").read_text())
    assert m["files"] == ref["files"]
