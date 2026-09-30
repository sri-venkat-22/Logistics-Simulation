"""Generate static mock JSON for the Level-1 clickable prototype (apps/web/src/mock).

Every figure produced here is PROTOTYPE MOCK DATA (seeded, deterministic). The
real values come from the Phase 3+ engines; nothing here may be quoted on stage.

    python3 data/generate_network.py && python3 scripts/gen_mock.py
"""
from __future__ import annotations

import json
import math
import urllib.request
from pathlib import Path

import networkx as nx
import numpy as np

ROOT = Path(__file__).resolve().parents[1]
DATA = ROOT / "data"
OUT = ROOT / "apps" / "web" / "src" / "mock"
OUT.mkdir(parents=True, exist_ok=True)
rng = np.random.default_rng(42)

nodes = json.loads((DATA / "nodes.json").read_text())
lanes = json.loads((DATA / "lanes.json").read_text())
skus = json.loads((DATA / "skus.json").read_text())
by_id = {n["id"]: n for n in nodes}


def dump(name: str, obj) -> None:
    (OUT / f"{name}.json").write_text(json.dumps(obj, separators=(",", ":")))
    print(f"  {name}.json  {(OUT / f'{name}.json').stat().st_size / 1024:.1f} KB")


# ---------------------------------------------------------------- network ---
G = nx.DiGraph()
for n in nodes:
    G.add_node(n["id"])
for l in lanes:
    G.add_edge(l["from_id"], l["to_id"], weight=l["lt_mean_h"])
btw = nx.betweenness_centrality(G.to_undirected(), weight="weight", normalized=True)

inventory = {}
for n in nodes:
    if n["type"] not in ("dc", "plant"):
        continue
    rows = []
    for s in skus:
        if s["cold_chain"] and n["type"] == "dc" and not n["attrs"].get("cold_chain"):
            continue
        cap = int(n["capacity"] * rng.uniform(0.25, 0.45))
        safety = int(cap * 0.22)
        on_hand = int(cap * rng.uniform(0.35, 0.85))
        rows.append({"sku": s["id"], "on_hand": on_hand, "safety": safety, "capacity": cap,
                     "on_order": int(cap * rng.uniform(0.05, 0.3)), "backorder": 0})
    inventory[n["id"]] = rows
# the demo hero: Shamshabad pharma DC is lean on vaccines
for r in inventory["DC_HYD_SHAMSHABAD"]:
    if r["sku"] == "SKU_VAX":
        r["on_hand"] = int(r["capacity"] * 0.41)

node_out = []
for n in nodes:
    tts = ttr = None
    if n["type"] in ("dc", "port", "plant", "supplier"):
        ttr = round(float(rng.uniform(2.0, 9.0)), 1)
        tts = round(float(rng.uniform(1.5, 12.0)), 1)
    node_out.append({**n, "betweenness": round(btw[n["id"]], 4), "degree": G.degree(n["id"]),
                     "tts_d": tts, "ttr_d": ttr})
hero = next(x for x in node_out if x["id"] == "DC_HYD_SHAMSHABAD")
hero["tts_d"], hero["ttr_d"] = 3.2, 5.0
chn = next(x for x in node_out if x["id"] == "PORT_CHENNAI")
chn["tts_d"], chn["ttr_d"] = 2.1, 5.0

dump("network", {"nodes": node_out, "lanes": lanes, "skus": skus, "inventory": inventory})

# ------------------------------------------------------- india shipments ---
statuses = ["in_transit"] * 7 + ["at_risk"] * 2 + ["delayed"]
shipments = []
for i in range(160):
    l = lanes[int(rng.integers(0, len(lanes)))]
    sku = skus[int(rng.integers(0, 3))]
    eta = float(l["lt_mean_h"] * rng.uniform(0.1, 1.0))
    st = statuses[int(rng.integers(0, len(statuses)))]
    if l["from_id"] == "PORT_CHENNAI" or l["to_id"] == "PORT_CHENNAI":
        st = "at_risk"
    shipments.append({
        "id": f"SH-{24100 + i}", "lane_id": l["id"], "sku_id": sku["id"],
        "qty": int(rng.integers(40, 900)), "progress": round(float(rng.uniform(0.02, 0.98)), 3),
        "speed": round(float(rng.uniform(0.6, 1.4)), 2), "status": st,
        "eta_h": round(eta, 1), "eta_p10_h": round(eta * 0.82, 1), "eta_p90_h": round(eta * 1.35, 1),
        "vehicle_id": f"{l['mode'][0].upper()}{rng.integers(1000, 9999)}",
    })
dump("shipments", shipments)

# ---------------------------------------------------------- hyderabad ---
HUBS = {
    "Medchal DC": (17.630, 78.480), "Shamshabad DC": (17.240, 78.430), "Patancheru Plant": (17.530, 78.260),
    "Gachibowli": (17.440, 78.348), "Kukatpally": (17.494, 78.399), "Uppal": (17.405, 78.559),
    "LB Nagar": (17.347, 78.552), "Secunderabad": (17.439, 78.498), "Kompally": (17.536, 78.487),
    "Jeedimetla": (17.515, 78.455), "Hitec City": (17.447, 78.376), "Kothur Pharma": (17.160, 78.290),
    "Mehdipatnam": (17.395, 78.437), "Ghatkesar": (17.450, 78.685),
}
PAIRS = [
    ("Patancheru Plant", "Shamshabad DC"), ("Patancheru Plant", "Medchal DC"), ("Medchal DC", "Uppal"),
    ("Medchal DC", "Gachibowli"), ("Medchal DC", "Secunderabad"), ("Shamshabad DC", "LB Nagar"),
    ("Shamshabad DC", "Hitec City"), ("Shamshabad DC", "Mehdipatnam"), ("Kothur Pharma", "Shamshabad DC"),
    ("Jeedimetla", "Kukatpally"), ("Kompally", "Ghatkesar"), ("Patancheru Plant", "Kukatpally"),
    ("Medchal DC", "Shamshabad DC"), ("Gachibowli", "LB Nagar"), ("Secunderabad", "Shamshabad DC"),
    ("Uppal", "Hitec City"),
]
cache = DATA / "hyderabad_routes.json"
if cache.exists():
    routes = json.loads(cache.read_text())
else:
    routes = []
    for a, b in PAIRS:
        (la1, lo1), (la2, lo2) = HUBS[a], HUBS[b]
        url = (f"https://router.project-osrm.org/route/v1/driving/{lo1},{la1};{lo2},{la2}"
               "?overview=full&geometries=geojson")
        with urllib.request.urlopen(url, timeout=20) as r:
            res = json.load(r)["routes"][0]
        coords = [[round(x, 5), round(y, 5)] for x, y in res["geometry"]["coordinates"]][::3]
        routes.append({"from": a, "to": b, "distance_m": res["distance"], "duration_s": res["duration"],
                       "path": coords})
        print(f"  osrm {a} -> {b}: {res['distance']/1000:.1f} km")
    cache.write_text(json.dumps(routes))
# the flood demo: the Medchal -> Shamshabad corridor floods; trucks detour east via Uppal / LB Nagar
detour_cache = DATA / "hyderabad_detour.json"
if detour_cache.exists():
    detour = json.loads(detour_cache.read_text())
else:
    (la1, lo1), (la2, lo2) = HUBS["Medchal DC"], HUBS["Shamshabad DC"]
    (wa1, wo1), (wa2, wo2) = HUBS["Uppal"], HUBS["LB Nagar"]
    url = (f"https://router.project-osrm.org/route/v1/driving/{lo1},{la1};{wo1},{wa1};{wo2},{wa2};{lo2},{la2}"
           "?overview=full&geometries=geojson")
    with urllib.request.urlopen(url, timeout=20) as r:
        res = json.load(r)["routes"][0]
    detour = {"from": "Medchal DC", "to": "Shamshabad DC", "distance_m": res["distance"], "duration_s": res["duration"],
              "path": [[round(x, 5), round(y, 5)] for x, y in res["geometry"]["coordinates"]][::3]}
    detour_cache.write_text(json.dumps(detour))
    print(f"  osrm detour: {res['distance']/1000:.1f} km")
flood_idx = PAIRS.index(("Medchal DC", "Shamshabad DC"))
flood_path = routes[flood_idx]["path"]
dump("hyderabad", {"hubs": [{"name": k, "lat": v[0], "lon": v[1]} for k, v in HUBS.items()],
                   "routes": routes, "flood_route_index": flood_idx, "detour": detour,
                   "flood_point": flood_path[int(len(flood_path) * 0.55)]})

# ------------------------------------------------ scenario monte carlo ---
DAYS, REPS = 30, 500


def simulate(closure_days: float, mitigation: str, reps: int) -> np.ndarray:
    """Toy daily (s,S) inventory model of vaccines at DC_HYD_SHAMSHABAD."""
    out = np.zeros((reps, DAYS * 4 + 1))
    for r in range(reps):
        rr = np.random.default_rng(1000 + r)  # common random numbers across plans
        cl = closure_days * rr.lognormal(0, 0.25)
        # one inbound was due via Chennai on day 1: held at the port (B, C) or diverted via Vizag (A)
        inbound_eta = 2.2 if mitigation == "A" else cl + 0.8
        inv, pipeline = 330.0, [(inbound_eta, 400.0)]
        for t in range(DAYS * 4 + 1):
            day = t / 4
            demand = rr.gamma(9, 10.8) / 4 * (1.25 if 3 <= day <= 9 else 1.0)
            arrived = sum(q for (eta, q) in pipeline if eta <= day)
            pipeline = [(eta, q) for (eta, q) in pipeline if eta > day]
            inv = max(0.0, inv + arrived - demand)
            if t % 4 == 0 and inv + sum(q for _, q in pipeline) < 900:
                lead = rr.lognormal(math.log(2.6), 0.3)
                if day <= cl:  # Chennai port closed
                    lead += (cl - day) + rr.uniform(0.5, 1.5)
                    if mitigation == "A":
                        lead = rr.lognormal(math.log(3.1), 0.25)  # via Vizag
                    elif mitigation == "B":
                        lead = rr.lognormal(math.log(0.6), 0.2)  # air
                pipeline.append((day + lead, 650.0))
            if mitigation == "A" and abs(day - 1.25) < 1e-9:
                pipeline.append((day + 0.6, 1200.0))  # Bengaluru transfer, ~14 h
            out[r, t] = inv
    return out


def bands(arr: np.ndarray) -> dict:
    p = np.percentile(arr, [10, 50, 90], axis=0)
    return {"p10": p[0].round(0).tolist(), "p50": p[1].round(0).tolist(), "p90": p[2].round(0).tolist()}


base = simulate(5, "C", REPS)
plan_a = simulate(5, "A", REPS)
plan_b = simulate(5, "B", REPS)
t_axis = [round(t / 4, 2) for t in range(DAYS * 4 + 1)]


def tts(arr: np.ndarray) -> float | None:
    """Median time-to-stockout in days, None if the P50 path never stocks out."""
    p50 = np.percentile(arr, 50, axis=0)
    idx = int(np.argmax(p50 < 1))
    return round(t_axis[idx], 1) if p50[idx] < 1 else None


def stockout_prob(arr: np.ndarray) -> float:
    return round(float((arr.min(axis=1) < 1).mean()), 3)


templates = [
    {"type": "port_closure", "label": "Port closure", "icon": "anchor", "target": "PORT_CHENNAI", "duration_h": 120, "severity": 1.0},
    {"type": "cyclone", "label": "Cyclone", "icon": "tornado", "target": "PORT_CHENNAI", "duration_h": 120, "severity": 0.9},
    {"type": "road_flood", "label": "Road flood", "icon": "waves", "target": "DC_HYD_SHAMSHABAD", "duration_h": 36, "severity": 0.8},
    {"type": "demand_spike", "label": "Demand spike", "icon": "trending-up", "target": "Z_HYD", "duration_h": 168, "severity": 0.6},
    {"type": "supplier_failure", "label": "Supplier failure", "icon": "factory", "target": "PLANT_PATANCHERU", "duration_h": 72, "severity": 1.0},
    {"type": "strike", "label": "Strike", "icon": "hand", "target": "DC_NAGPUR", "duration_h": 48, "severity": 0.7},
    {"type": "data_blackout", "label": "Data blackout", "icon": "wifi-off", "target": "*", "duration_h": 2, "severity": 0.3},
]
# Pareto cloud of candidate plans (cost ₹L vs service %)
cands = []
for i in range(46):
    cost = float(rng.uniform(6, 95))
    service = 99.4 - 20 * math.exp(-cost / 22) - float(rng.uniform(0, 6))
    co2 = float(rng.uniform(4, 60))
    cands.append({"id": f"cand-{i}", "cost_lakh": round(cost, 1), "service": round(service, 1), "co2_t": round(co2, 1)})
plans = [
    {"id": "A", "name": "Reroute via Vizag + transfer from Bengaluru", "actions": [
        "Divert 3 inbound vessels PORT_CHENNAI → PORT_VIZAG",
        "Transfer 1,200 vaccine units DC_BLR → DC_HYD_SHAMSHABAD (reefer road, ETA 14 h)",
        "Raise Shamshabad safety stock +18% for 10 days"],
     "cost_lakh": 18.4, "service": 97.8, "co2_t": 21.6, "cvar95_lost": 310, "stockout_p": stockout_prob(plan_a), "tts_d": tts(plan_a), "score": 0.86,
     "explain": "Plan A keeps Shamshabad above safety stock by moving 1,200 vaccine units from Bengaluru, arriving in ~14 h, and shifts ocean inbound to Vizag while Chennai recovers."},
    {"id": "B", "name": "Air-expedite critical SKUs", "actions": [
        "Air-freight vaccines PLANT_PATANCHERU → DC_HYD_SHAMSHABAD daily during closure",
        "Hold electronics inbound at anchorage"],
     "cost_lakh": 61.0, "service": 98.9, "co2_t": 58.2, "cvar95_lost": 140, "stockout_p": stockout_prob(plan_b), "tts_d": tts(plan_b), "score": 0.64,
     "explain": "Plan B has the best service level but costs 3.3× Plan A and emits 2.7× the CO₂."},
    {"id": "C", "name": "Do nothing (baseline)", "actions": ["Wait for Chennai port to reopen (TTR ≈ 5 d)"],
     "cost_lakh": 0.0, "service": 81.6, "co2_t": 9.1, "cvar95_lost": 2240, "stockout_p": stockout_prob(base), "tts_d": tts(base), "score": 0.21,
     "explain": "The network is exposed: TTS at Shamshabad (≈3.2 d) is shorter than Chennai's TTR (≈5 d)."},
]
cyclone_poly = [[80.05, 12.55], [80.9, 12.7], [81.25, 13.3], [80.95, 13.9], [80.3, 13.95], [79.95, 13.5], [79.9, 12.95]]
dump("scenario", {"templates": templates, "t_days": t_axis, "reps": REPS, "node": "DC_HYD_SHAMSHABAD", "sku": "SKU_VAX",
                  "safety_stock": 180, "baseline": bands(base), "mitigated": bands(plan_a), "air": bands(plan_b),
                  "tts_d": tts(base), "ttr_d": 5.0, "plans": plans, "candidates": cands, "cyclone_polygon": cyclone_poly,
                  "disrupted_lanes": [l["id"] for l in lanes if "PORT_CHENNAI" in (l["from_id"], l["to_id"])],
                  "reroute_lanes": [l["id"] for l in lanes if (l["from_id"], l["to_id"]) in
                                    {("SUP_SHENZHEN", "PORT_VIZAG"), ("PORT_VIZAG", "DC_HYD_SHAMSHABAD"),
                                     ("DC_BLR", "DC_HYD_SHAMSHABAD"), ("PORT_VIZAG", "DC_HYD_MEDCHAL")}]})

# ---------------------------------------------------------- control tower ---
hours = list(range(-48, 73, 2))
risk = [round(max(0.0, 4.8 + 0.02 * h + (0 if h < 6 else 0.09 * (h - 6)) + float(rng.normal(0, 0.15))), 2) for h in hours]
dump("control", {
    "kpis": [
        {"id": "fill", "label": "Fill rate", "value": 96.4, "unit": "%", "delta": -0.6, "state": "ok", "spark": [97.1, 97.0, 96.9, 96.8, 96.9, 96.6, 96.5, 96.4]},
        {"id": "otif", "label": "OTIF", "value": 91.2, "unit": "%", "delta": -1.1, "state": "warn", "spark": [92.8, 92.6, 92.4, 92.0, 91.9, 91.5, 91.4, 91.2]},
        {"id": "risk", "label": "At-risk shipments", "value": 37, "unit": "", "delta": 12, "state": "warn", "spark": [18, 19, 22, 24, 25, 29, 33, 37]},
        {"id": "inr", "label": "₹ at risk", "value": 4.8, "unit": "Cr", "delta": 1.9, "state": "bad", "spark": [2.1, 2.3, 2.4, 2.9, 3.3, 3.8, 4.4, 4.8]},
    ],
    "alerts": [
        {"id": "a1", "sev": "bad", "t": "2 min ago", "title": "Cyclone warning · Bay of Bengal", "body": "IMD: landfall near Chennai in ~30 h. PORT_CHENNAI closure likely (P=0.78).", "node": "PORT_CHENNAI"},
        {"id": "a2", "sev": "warn", "t": "6 min ago", "title": "Shamshabad DC · vaccine TTS 3.2 d", "body": "TTS < TTR of Chennai port (5.0 d). Network exposed.", "node": "DC_HYD_SHAMSHABAD"},
        {"id": "a3", "sev": "sec", "t": "9 min ago", "title": "GPS teleport quarantined", "body": "Truck R4471 jumped 612 km in 4 s. Kalman estimate kept. Source trust 0.91 → 0.62.", "node": "DC_HYD_MEDCHAL"},
        {"id": "a4", "sev": "warn", "t": "14 min ago", "title": "ORR Exit 16 congestion", "body": "SUMO travel time +38% on Medchal → Shamshabad corridor.", "node": "DC_HYD_MEDCHAL"},
        {"id": "a5", "sev": "ai", "t": "15 min ago", "title": "Copilot suggests a what-if", "body": "\"Simulate a 5-day Chennai closure?\" — open Scenario Lab.", "node": None},
        {"id": "a6", "sev": "ok", "t": "22 min ago", "title": "Nagpur hub recovered", "body": "Strike ended; throughput back to 98% of plan.", "node": "DC_NAGPUR"},
        {"id": "a7", "sev": "sec", "t": "31 min ago", "title": "Supplier ASN rejected", "body": "SUP_CHENNAI_AUTO claimed 1,000,000 units (MAD z = 41).", "node": "SUP_CHENNAI_AUTO"},
    ],
    "timeline": {"hours": hours, "inr_at_risk_cr": risk},
    "weather": [
        {"name": "Chennai", "lat": 13.08, "lon": 80.27, "wind_kmh": 74, "rain_mm": 112, "code": "cyclone"},
        {"name": "Visakhapatnam", "lat": 17.69, "lon": 83.22, "wind_kmh": 31, "rain_mm": 18, "code": "rain"},
        {"name": "Hyderabad", "lat": 17.38, "lon": 78.48, "wind_kmh": 14, "rain_mm": 4, "code": "cloud"},
        {"name": "Bengaluru", "lat": 12.97, "lon": 77.59, "wind_kmh": 12, "rain_mm": 2, "code": "cloud"},
        {"name": "Mumbai", "lat": 19.07, "lon": 72.87, "wind_kmh": 18, "rain_mm": 0, "code": "clear"},
    ],
    "ships": [{"id": f"IMO{9300000 + i}", "lat": float(lat), "lon": float(lon), "heading": float(h)} for i, (lat, lon, h) in enumerate([
        (12.6, 81.4, 290), (11.9, 82.3, 300), (13.4, 81.8, 260), (16.8, 84.6, 250), (17.2, 85.4, 240),
        (18.6, 71.9, 70), (19.8, 71.2, 80), (21.9, 68.6, 60), (20.9, 88.4, 350), (10.2, 79.9, 20),
        (14.5, 82.8, 280), (8.5, 77.2, 110)])],
})

# ----------------------------------------------------------------- trust ---
LAYERS = ["Schema", "Authenticity", "Temporal", "Physics", "Map-match", "Kalman", "Twin oracle", "Feed anomaly", "Reputation"]
layer_counts = [1843, 212, 604, 97, 41, 63, 18, 129, 22]
sources = []
for i, (sid, typ) in enumerate([("gps-fleet-hyd", "gps"), ("gps-fleet-south", "gps"), ("gps-fleet-west", "gps"),
                                  ("wms-medchal", "inventory"), ("wms-shamshabad", "inventory"), ("wms-blr", "inventory"),
                                  ("asn-sup-chennai-auto", "supplier"), ("asn-patancheru", "supplier"), ("asn-shenzhen", "supplier"),
                                  ("port-chennai", "port"), ("port-vizag", "port"), ("open-meteo", "weather")]):
    trust = round(float(rng.uniform(0.86, 0.99)), 2)
    if sid == "gps-fleet-hyd":
        trust = 0.62
    if sid == "asn-sup-chennai-auto":
        trust = 0.31
    sources.append({"id": sid, "type": typ, "trust": trust, "msgs_min": int(rng.integers(40, 4200)),
                    "last_seen_s": int(rng.integers(0, 8)), "sla_s": 30,
                    "state": "quarantine" if trust < 0.4 else ("degraded" if trust < 0.7 else "ok")})
REASONS = [("PHYSICS_TELEPORT", "Physics", "Δd/Δt = 551,000 km/h"), ("SCHEMA_NAN_QTY", "Schema", "qty = NaN"),
           ("HMAC_INVALID", "Authenticity", "signature mismatch"), ("REPLAY_NONCE", "Temporal", "nonce reused within 60 s"),
           ("STALE_TS", "Temporal", "ts 47 min behind"), ("KALMAN_GATE", "Kalman", "Mahalanobis d² = 38.2 > χ²₀.₉₉"),
           ("OFFROAD", "Map-match", "412 m from nearest edge"), ("ASN_OUTLIER", "Feed anomaly", "MAD z = 41.0"),
           ("NEG_QTY", "Schema", "on_hand = -340"), ("TWIN_ENVELOPE", "Twin oracle", "outside P99 route envelope")]
qlog = []
for i in range(40):
    code, layer, detail = REASONS[int(rng.integers(0, len(REASONS)))]
    src = sources[int(rng.integers(0, len(sources)))]["id"]
    qlog.append({"id": f"Q-{88120 + i}", "t": f"12:{59 - i:02d}:{int(rng.integers(0, 60)):02d}", "source": src, "code": code, "layer": layer, "detail": detail})
qlog[0] = {"id": "Q-88120", "t": "12:59:41", "source": "gps-fleet-hyd", "code": "PHYSICS_TELEPORT", "layer": "Physics", "detail": "R4471 jumped 612 km in 4 s (Hyderabad → Mumbai)"}
qlog[1] = {"id": "Q-88121", "t": "12:58:03", "source": "asn-sup-chennai-auto", "code": "ASN_OUTLIER", "layer": "Feed anomaly", "detail": "ASN claims 1,000,000 units (MAD z = 41.0)"}
# attack timeline: minute buckets of clean vs rejected
tl = [{"m": m, "clean": int(4800 + rng.normal(0, 180)), "rejected": int(abs(rng.normal(12, 5)))} for m in range(60)]
for m in range(38, 44):
    tl[m]["rejected"] += int(rng.integers(300, 700))
for m in range(50, 56):
    tl[m]["clean"] = int(tl[m]["clean"] * 0.7)
spoof = {"vehicle": "R4471", "true": [[78.47, 17.61], [78.465, 17.585], [78.458, 17.56], [78.452, 17.535], [78.447, 17.51], [78.44, 17.485]],
         "reported": [[78.47, 17.61], [78.465, 17.585], [72.88, 19.07], [72.87, 19.08], [78.447, 17.51], [78.44, 17.485]]}
dump("trust", {"layers": [{"name": n, "rejected": c} for n, c in zip(LAYERS, layer_counts)], "sources": sources,
               "quarantine": qlog, "timeline": tl, "spoof": spoof,
               "counters": {"msgs_24h": 6_912_440, "quarantined_24h": 3029, "attacks_detected": 412, "mttd_s": 1.4, "crashes": 0},
               "chaos": [
                   {"id": "gps_teleport", "label": "GPS teleport", "desc": "Truck jumps 600 km", "icon": "map-pin-off"},
                   {"id": "drift", "label": "Slow drift spoof", "desc": "1 m/s bias for 10 min", "icon": "move-diagonal"},
                   {"id": "asn_inflate", "label": "Inflated ASN", "desc": "Supplier claims 1,000,000 units", "icon": "package-x"},
                   {"id": "replay", "label": "Replay attack", "desc": "Re-send signed batch", "icon": "repeat"},
                   {"id": "malformed", "label": "Malformed flood", "desc": "30% NaN / missing fields", "icon": "file-warning"},
                   {"id": "blackout", "label": "30% blackout", "desc": "Drop 30% of sources, 10 min", "icon": "wifi-off"},
                   {"id": "cascade", "label": "Cascade failure", "desc": "Fail DC_NAGPUR + dependents", "icon": "git-fork"},
               ]})

# -------------------------------------------------------------- fidelity ---
n = 72
actual = np.cumsum(rng.normal(0, 1.2, n)) + 22
pred = actual + rng.normal(0, 1.1, n)
width = 2.2 + rng.uniform(0, 0.8, n)
bins = [0.1, 0.2, 0.3, 0.4, 0.5, 0.6, 0.7, 0.8, 0.9]
reliab = [round(float(min(0.99, max(0.01, b + rng.normal(0, 0.03)))), 3) for b in bins]
ATT = ["Teleport", "Drift", "Replay", "Malformed", "ASN inflate", "Clean"]
cm = []
for i, a in enumerate(ATT):
    row = [int(rng.integers(0, 4)) for _ in ATT]
    row[i] = int(rng.integers(80, 118)) if a != "Clean" else 2940
    if a == "Drift":
        row[i], row[-1] = 71, 14
    if a == "Clean":
        row = [3, 9, 0, 1, 4, 2940]
    cm.append(row)
dump("fidelity", {
    "metrics": [
        {"id": "mape", "label": "ETA MAPE", "value": 8.7, "unit": "%", "target": "< 12%", "state": "ok"},
        {"id": "cov", "label": "P10–P90 coverage", "value": 78.9, "unit": "%", "target": "≈ 80%", "state": "ok"},
        {"id": "f1", "label": "Attack F1", "value": 0.94, "unit": "", "target": "> 0.90", "state": "ok"},
        {"id": "value", "label": "Lost sales avoided", "value": 31, "unit": "%", "target": "vs no-action", "state": "ai"},
    ],
    "eta": {"t": list(range(n)), "actual": actual.round(2).tolist(), "pred": pred.round(2).tolist(),
            "p10": (pred - width).round(2).tolist(), "p90": (pred + width).round(2).tolist()},
    "reliability": {"nominal": bins, "observed": reliab},
    "confusion": {"labels": ATT, "matrix": cm},
    "decision_value": [
        {"policy": "No action", "lost_sales": 100, "cost": 100, "co2": 100},
        {"policy": "Naïve rule", "lost_sales": 82, "cost": 104, "co2": 101},
        {"policy": "AEGIS", "lost_sales": 69, "cost": 91, "co2": 94},
    ],
    "wasserstein": [{"corridor": c, "w1_min": round(float(rng.uniform(1.2, 9.5)), 1)} for c in
                    ["Medchal→Shamshabad", "Patancheru→Medchal", "Kothur→Shamshabad", "Uppal→Hitec", "Secunderabad→Shamshabad"]],
    "drift": {"state": "recalibrated", "detail": "Rolling ETA MAPE on Medchal→Shamshabad hit 14.2% at 11:40; lane lead-time posterior updated (μ +6.1%); MAPE back to 8.9%."},
})

# --------------------------------------------------------------------- ops ---
t60 = list(range(60))
dump("ops", {
    "pods": [
        {"name": "api-7f9c4b-x2kq", "svc": "api", "status": "Running", "restarts": 0, "cpu": 38, "mem": 41, "age": "3h"},
        {"name": "api-7f9c4b-m8tz", "svc": "api", "status": "Running", "restarts": 0, "cpu": 35, "mem": 39, "age": "3h"},
        {"name": "ingest-5d8b6-q1wd", "svc": "ingest", "status": "Running", "restarts": 0, "cpu": 71, "mem": 33, "age": "3h"},
        {"name": "ingest-5d8b6-v7rp", "svc": "ingest", "status": "Running", "restarts": 0, "cpu": 68, "mem": 31, "age": "47m"},
        {"name": "trust-worker-6c-4hz", "svc": "trust-worker", "status": "Running", "restarts": 1, "cpu": 55, "mem": 48, "age": "12m"},
        {"name": "trust-worker-6c-9kd", "svc": "trust-worker", "status": "Running", "restarts": 0, "cpu": 58, "mem": 47, "age": "3h"},
        {"name": "twin-state-0", "svc": "twin-state", "status": "Running", "restarts": 0, "cpu": 44, "mem": 62, "age": "3h"},
        {"name": "sim-worker-8b-2mn", "svc": "sim-worker", "status": "Running", "restarts": 0, "cpu": 92, "mem": 57, "age": "4m"},
        {"name": "sim-worker-8b-7xq", "svc": "sim-worker", "status": "ContainerCreating", "restarts": 0, "cpu": 0, "mem": 0, "age": "6s"},
        {"name": "sumo-runner-0", "svc": "sumo-runner", "status": "Running", "restarts": 0, "cpu": 81, "mem": 36, "age": "3h"},
        {"name": "redis-0", "svc": "redis", "status": "Running", "restarts": 0, "cpu": 22, "mem": 28, "age": "3h"},
        {"name": "timescale-0", "svc": "postgres", "status": "Running", "restarts": 0, "cpu": 31, "mem": 54, "age": "3h"},
    ],
    "loadtest": {"tool": "k6", "target_msgs_s": 5000, "peak_msgs_s": 7420, "p95_ms": 612, "p99_ms": 890, "error_rate": 0.0004, "vcpu": 4,
                 "ramp": [{"vus": v, "msgs_s": m, "p95_ms": p} for v, m, p in
                          [(50, 1200, 180), (100, 2500, 240), (200, 4100, 330), (300, 5600, 450), (400, 6800, 560), (500, 7420, 612), (600, 7510, 1340)]]},
    "series": {"t": t60,
               "ingest": [int(5200 + 600 * math.sin(i / 7) + rng.normal(0, 120)) for i in t60],
               "p95": [int(420 + 80 * math.sin(i / 9) + rng.normal(0, 25)) for i in t60],
               "queue": [int(max(0, 40 + 30 * math.sin(i / 5) + rng.normal(0, 8))) for i in t60],
               "ws_clients": [int(38 + i // 6) for i in t60]},
    "slo": {"availability": 99.83, "target": 99.5, "error_budget_left": 66},
})

# ----------------------------------------------------------------- copilot ---
dump("copilot", {"script": [
    {"role": "user", "text": "What if a cyclone closes Chennai port for 5 days?"},
    {"role": "tool", "name": "create_scenario", "args": '{"type":"cyclone","target":"PORT_CHENNAI","start":"+6h","duration_h":120,"severity":0.9}', "result": "scenario sc_7f3a created"},
    {"role": "tool", "name": "run_scenario", "args": '{"id":"sc_7f3a","n_reps":500}', "result": "500 / 500 replications · 9.6 s"},
    {"role": "assistant", "text": "Under a 5-day closure, **Hyderabad-Shamshabad** runs out of vaccines in **3.2 days (TTS)** at the median, but Chennai port needs **~5 days to recover (TTR)**. Stock-out probability is **{p}%** without action.".replace("{p}", str(round(plans[2]["stockout_p"] * 100)))},
    {"role": "tool", "name": "optimize", "args": '{"id":"sc_7f3a","objectives":["cost","service","co2"]}', "result": "46 candidates · 3 on Pareto front"},
    {"role": "assistant", "text": "I recommend **Plan A — reroute via Vizag + transfer 1,200 units from Bengaluru**. It lifts service to 97.8% for ₹18.4 L, a third of the air-expedite cost. Evidence: event log rows 1,204–1,391, Monte Carlo seed 1000–1499."},
    {"role": "proposal", "plan": "A"},
]})
print("done")
