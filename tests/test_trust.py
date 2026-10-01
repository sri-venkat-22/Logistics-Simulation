"""Phase 7.5: trust layers 5-9 (map-matching, Kalman gate, twin oracle, feed anomaly, reputation) and the
red-team benchmark score of the whole pipeline."""
import math
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

import pytest

from services.api.app.trust_layers import (FeedAnomaly, KalmanGate, Reputation, RoadIndex, TwinOracle, M_PER_DEG,
                                           load_city_cells)
from sim.macro.network import Network

T0 = datetime(2026, 10, 20, 10, tzinfo=ZoneInfo("Asia/Kolkata"))


@pytest.fixture(scope="module")
def net():
    return Network()


def fix(vid, lat, lon, speed_kmh, heading, t, scope="national", hdop=1.0, shipment=None):
    return {"vehicle_id": vid, "lat": lat, "lon": lon, "speed_kmh": speed_kmh, "heading_deg": heading, "hdop": hdop,
            "scope": scope, "shipment_id": shipment}, t


def track(lat0, lon0, heading, speed_kmh, n, dt_s=5.0, drift_mps=0.0, drift_from=10**9, drift_bearing=90.0,
          observed_scale=0.9, noise_m=4.0, seed=1):
    """A straight national track: the odometer says speed_kmh, the fixes move at observed_scale of it (road
    distance > straight line), plus GPS noise, plus an optional drift that starts at step drift_from."""
    import random
    rnd = random.Random(seed)
    hd, db = math.radians(heading), math.radians(drift_bearing)
    v = speed_kmh / 3.6 * observed_scale
    out = []
    for i in range(n):
        t = i * dt_s
        e, nn = v * math.sin(hd) * t, v * math.cos(hd) * t
        if i >= drift_from:
            d = drift_mps * (i - drift_from) * dt_s
            e, nn = e + d * math.sin(db), nn + d * math.cos(db)
        e += rnd.gauss(0, noise_m)
        nn += rnd.gauss(0, noise_m)
        lat = lat0 + nn / M_PER_DEG
        lon = lon0 + e / (M_PER_DEG * math.cos(math.radians(lat0)))
        out.append(fix("TRK-1", lat, lon, speed_kmh, heading, T0 + timedelta(seconds=t)))
    return out


def test_kalman_accepts_honest_track_with_road_geometry_bias():
    g = KalmanGate()
    verdicts = [g.check(p, t) for p, t in track(17.0, 79.0, 40.0, 60.0, 400)]
    assert not any(verdicts)


def test_kalman_catches_drift_that_starts_mid_segment():
    g = KalmanGate()
    pts = track(17.0, 79.0, 40.0, 60.0, 400, drift_mps=1.0, drift_from=100, drift_bearing=130.0)
    verdicts = [g.check(p, t) for p, t in pts]
    assert not any(verdicts[:100])
    first = next(i for i, v in enumerate(verdicts) if v)
    assert verdicts[first] == ("L6", "KALMAN_GATE") and first - 100 < 24   # within 2 minutes at 5 s fixes


def test_kalman_flags_drift_absorbed_from_the_first_fix():
    g = KalmanGate()
    pts = track(17.0, 79.0, 40.0, 60.0, 200, drift_mps=2.0, drift_from=0, drift_bearing=130.0)  # sideways from the start
    assert any(g.check(p, t) for p, t in pts)


def test_kalman_accepts_the_honest_track_again_after_a_drift_ends():
    g = KalmanGate()
    pts = track(17.0, 79.0, 40.0, 60.0, 300, drift_mps=2.0, drift_from=100, drift_bearing=130.0)
    honest = track(17.0, 79.0, 40.0, 60.0, 400, seed=2)
    seq = pts[:160] + honest[160:400]  # the drift stops at step 160: fixes snap back onto the true road
    verdicts = [g.check(p, t) for p, t in seq]
    assert any(verdicts[100:160])
    assert sum(1 for v in verdicts[160:] if v) <= 5


def test_road_index_city_and_national(net):
    cells = load_city_cells(build=False)
    if cells is None:
        pytest.skip("road_cells.npz not built")
    r = RoadIndex(net, cells)
    hub = {"lat": 17.24, "lon": 78.43}  # Shamshabad DC hub (snapped to Terminal 1 Approach Rd)
    assert any(r.on_road(hub["lat"] + dl, hub["lon"] + dl, "city") for dl in (0.0, 0.0005, -0.0005))
    assert not r.on_road(17.40, 78.60, "city")          # east of the micro-twin: no SUMO road there
    a, b = net.nodes["DC_BLR"], net.nodes["DC_HYD_SHAMSHABAD"]
    mid = ((a.lat + b.lat) / 2, (a.lon + b.lon) / 2)
    assert r.check({"lat": mid[0], "lon": mid[1], "scope": "national"}) is None
    assert r.check({"lat": 10.0, "lon": 85.0, "scope": "national"}) == ("L5", "OFFROAD")  # Bay of Bengal


def test_twin_oracle_route_corridor(net):
    o = TwinOracle(net)
    o.learn_asn({"asn_id": "ASN-SH000777-1", "lanes": ["L025"]})     # DC_BLR -> DC_HYD_SHAMSHABAD
    a, b = net.nodes["DC_BLR"], net.nodes["DC_HYD_SHAMSHABAD"]
    on = {"lat": (a.lat + b.lat) / 2, "lon": (a.lon + b.lon) / 2, "scope": "national", "shipment_id": "SH000777"}
    assert o.check_gps(on) is None
    off = {**on, "lat": 19.0, "lon": 73.0}                              # near Pune: a plausible road, the wrong corridor
    assert o.check_gps(off) == ("L7", "TWIN_ENVELOPE")
    assert o.check_gps({**off, "shipment_id": "SH_UNKNOWN"}) is None    # no planned route known: no verdict


def test_feed_anomaly_asn_outlier_and_reconciliation():
    levels = {("DC_BLR", "SKU_VAX"): (800.0, 1500.0)}
    f = FeedAnomaly(levels=lambda dc, sku: levels.get((dc, sku)))
    asn = {"asn_id": "ASN-SH1-1", "supplier": "PLANT_PATANCHERU", "dc": "DC_BLR", "sku": "SKU_VAX", "qty": 380.0,
           "ship_ts": T0.isoformat(), "eta_ts": (T0 + timedelta(hours=20)).isoformat(), "lanes": ["L017"]}
    assert f.check_asn(asn) is None
    assert f.check_asn({**asn, "qty": 3800.0}) == ("L8", "ASN_OUTLIER")                      # 10x a truck-load
    assert f.check_asn({**asn, "eta_ts": (T0 - timedelta(hours=1)).isoformat()}) == ("L8", "ASN_OUTLIER")  # arrives before it ships
    f.accept_asn(asn)
    st = {"node": "DC_BLR", "sku": "SKU_VAX", "on_hand": 1200.0, "on_order": 0.0, "backlog": 0.0}
    assert f.check_stock(st, T0) is None
    f.accept_stock(st)
    assert f.check_stock({**st, "on_hand": 1500.0}, T0) is None                              # a receipt
    assert f.check_stock({**st, "on_hand": 1_000_000.0}, T0) == ("L8", "RECON_MISMATCH")      # "1,000,000 units"


def test_reputation_locks_out_persistent_offenders_and_recovers():
    r = Reputation()
    src = "wms:DC_X"                             # a stock feed: a few messages every 15 minutes
    t = T0
    for i in range(24):                          # six honest hours
        r.good(src, t + timedelta(minutes=15 * i))
    t += timedelta(hours=6)
    for i in range(30):                          # a burst within one minute counts once
        r.bad(src, "L8", t + timedelta(seconds=i))
    assert r.check(src) is None
    for i in range(12):                          # then every report is bad for an hour
        r.bad(src, "L8", t + timedelta(minutes=5 * (i + 1)))
    assert r.check(src) == ("L9", "LOW_REPUTATION")
    r.bad("gps:TRK", "L3", t)                    # replays / duplicates are not the device's fault
    assert r.trust("gps:TRK") == 1.0
    t += timedelta(hours=1)
    for i in range(8):                           # clean again: it earns its way back
        r.good(src, t + timedelta(minutes=5 * i))
    assert r.check(src) is None


def test_red_team_benchmark_score():
    """The whole pipeline (L1-L9) on the labelled benchmark: the numbers quoted in docs/trust/TRUST.md."""
    from services.api.app.trust_bench import BENCH, run
    if not (BENCH / "telemetry.jsonl.gz").exists():
        pytest.skip("benchmark not generated")
    r = run(progress=False)
    ml, bt = r["message_level"], r["by_type"]
    assert r["attack_level"]["recall"] >= 0.93
    assert ml["precision"] >= 0.95 and ml["false_positive_rate"] < 0.003
    for t in ("gps_teleport", "duplicate", "replay", "missing_fields", "nan_negative", "stale_timestamp", "inflated_asn"):
        assert bt[t]["attack_recall"] == 1.0, t
    assert bt["gps_drift"]["attack_recall"] >= 0.8 and bt["blackout"]["attack_recall"] >= 0.9
    assert bt["replay"]["caught_by"].get("L3:REPLAY_NONCE", 0) > 0.9 * bt["replay"]["msgs"]
