"""Generate the AEGIS Twin reference network: nodes, lanes, SKUs and sourcing paths.

    .venv/bin/python data/generate_network.py            # uses cached OSRM distances
    .venv/bin/python data/generate_network.py --refresh  # re-query OSRM for road lanes

Deterministic given the OSRM cache. Coordinates are real site locations (approximate to
~1 km). Lane distances:
  road  OSRM driving distance (router.project-osrm.org, cached in osrm_lanes.json);
        falls back to haversine x 1.3 if OSRM is unreachable
  rail  haversine x 1.3 (detour factor)
  sea   great-circle legs through real shipping waypoints (Malacca Strait, Dondra Head)
  air   haversine x 1.05 (airway routing)
Lead times are lognormal: mean = distance / effective speed + fixed handling/dwell,
sigma by mode. They are engineering priors, calibrated from SUMO (road, Hyderabad) in Phase 3.
"""
from __future__ import annotations

import argparse
import json
import math
import urllib.request
from pathlib import Path

import networkx as nx

OUT = Path(__file__).parent
OSRM_CACHE = OUT / "osrm_lanes.json"

# id, type, name, lat, lon, capacity (units/day: production, berth throughput or DC dispatch), attrs
NODES = [
    # Ports
    ("PORT_JNPT", "port", "JNPT / Nhava Sheva", 18.950, 72.950, 60000, {"berths": 6}),
    ("PORT_MUNDRA", "port", "Mundra Port", 22.740, 69.700, 55000, {"berths": 5}),
    ("PORT_CHENNAI", "port", "Chennai Port", 13.100, 80.300, 40000, {"berths": 4}),
    ("PORT_VIZAG", "port", "Visakhapatnam Port", 17.690, 83.280, 30000, {"berths": 3}),
    ("PORT_KOLKATA", "port", "Kolkata / Haldia", 22.030, 88.060, 28000, {"berths": 3}),
    # Plants / suppliers (produces = SKU ids made there)
    ("PLANT_PATANCHERU", "plant", "Patancheru Pharma API Plant", 17.530, 78.260, 6000,
     {"produces": ["SKU_VAX"], "industry": "pharma"}),
    ("SUP_CHENNAI_AUTO", "supplier", "Sriperumbudur Auto & Electronics Cluster", 12.960, 79.950, 4500,
     {"produces": ["SKU_ELEC"], "industry": "auto-parts / electronics"}),
    ("SUP_AHMEDABAD_TEX", "supplier", "Ahmedabad Textiles & FMCG", 23.020, 72.570, 30000,
     {"produces": ["SKU_FMCG"], "industry": "textiles / FMCG"}),
    ("SUP_PUNE_ELEC", "supplier", "Chakan Electronics", 18.760, 73.860, 3500,
     {"produces": ["SKU_ELEC"], "industry": "electronics"}),
    ("SUP_SHENZHEN", "supplier", "Shenzhen Electronics (overseas)", 22.540, 114.060, 12000,
     {"produces": ["SKU_ELEC"], "industry": "electronics", "overseas": True}),
    # Distribution centres (primary = families the DC is set up for; cold_chain = can hold vaccines)
    ("DC_HYD_MEDCHAL", "dc", "Hyderabad-Medchal DC (FMCG/e-com)", 17.630, 78.480, 18000,
     {"cold_chain": False, "primary": ["fmcg", "electronics"]}),
    ("DC_HYD_SHAMSHABAD", "dc", "Hyderabad-Shamshabad DC (pharma cold chain)", 17.240, 78.430, 9000,
     {"cold_chain": True, "primary": ["vaccine"], "near": "RGIA airport"}),
    ("DC_BLR", "dc", "Bengaluru-Hoskote DC", 13.070, 77.800, 16000, {"cold_chain": True}),
    ("DC_NAGPUR", "dc", "Nagpur Central Hub", 21.100, 79.050, 22000, {"cold_chain": False}),
    ("DC_DELHI", "dc", "Delhi-NCR (Gurugram) DC", 28.460, 77.030, 24000, {"cold_chain": True}),
    ("DC_PUNE", "dc", "Pune-Chakan DC", 18.700, 73.800, 14000, {"cold_chain": True}),
    # Demand zones (pop_m = urban agglomeration population, millions, approx. 2025)
    ("Z_HYD", "zone", "Hyderabad", 17.385, 78.487, 0, {"pop_m": 10.5}),
    ("Z_BLR", "zone", "Bengaluru", 12.972, 77.594, 0, {"pop_m": 13.6}),
    ("Z_CHN", "zone", "Chennai", 13.083, 80.270, 0, {"pop_m": 11.5}),
    ("Z_MUM", "zone", "Mumbai", 19.076, 72.878, 0, {"pop_m": 21.3}),
    ("Z_DEL", "zone", "Delhi", 28.614, 77.209, 0, {"pop_m": 32.9}),
    ("Z_KOL", "zone", "Kolkata", 22.573, 88.364, 0, {"pop_m": 15.3}),
    ("Z_PUN", "zone", "Pune", 18.520, 73.857, 0, {"pop_m": 7.2}),
    ("Z_AMD", "zone", "Ahmedabad", 23.023, 72.571, 0, {"pop_m": 8.6}),
    ("Z_VJA", "zone", "Vijayawada", 16.506, 80.648, 0, {"pop_m": 2.1}),
    ("Z_VSK", "zone", "Visakhapatnam", 17.686, 83.218, 0, {"pop_m": 2.4}),
    ("Z_CBE", "zone", "Coimbatore", 11.017, 76.956, 0, {"pop_m": 2.9}),
    ("Z_JAI", "zone", "Jaipur", 26.912, 75.787, 0, {"pop_m": 4.1}),
]

# from, to, mode. Order is stable: lane ids L001.. are referenced by the prototype and scenarios.
LANE_DEFS = [
    # inbound sea (overseas supplier)
    ("SUP_SHENZHEN", "PORT_CHENNAI", "sea"),
    ("SUP_SHENZHEN", "PORT_VIZAG", "sea"),
    ("SUP_SHENZHEN", "PORT_KOLKATA", "sea"),
    ("SUP_SHENZHEN", "PORT_JNPT", "sea"),
    # port -> DC
    ("PORT_CHENNAI", "DC_BLR", "road"),
    ("PORT_CHENNAI", "DC_HYD_SHAMSHABAD", "road"),
    ("PORT_CHENNAI", "DC_HYD_MEDCHAL", "rail"),
    ("PORT_VIZAG", "DC_HYD_MEDCHAL", "rail"),
    ("PORT_VIZAG", "DC_HYD_SHAMSHABAD", "road"),
    ("PORT_VIZAG", "DC_NAGPUR", "rail"),
    ("PORT_JNPT", "DC_PUNE", "road"),
    ("PORT_JNPT", "DC_NAGPUR", "rail"),
    ("PORT_MUNDRA", "DC_DELHI", "rail"),
    ("PORT_KOLKATA", "DC_NAGPUR", "rail"),
    # plants / suppliers -> DC
    ("PLANT_PATANCHERU", "DC_HYD_SHAMSHABAD", "road"),
    ("PLANT_PATANCHERU", "DC_HYD_MEDCHAL", "road"),
    ("PLANT_PATANCHERU", "DC_BLR", "road"),
    ("PLANT_PATANCHERU", "DC_DELHI", "air"),
    ("SUP_CHENNAI_AUTO", "DC_BLR", "road"),
    ("SUP_CHENNAI_AUTO", "DC_HYD_MEDCHAL", "road"),
    ("SUP_AHMEDABAD_TEX", "DC_PUNE", "road"),
    ("SUP_AHMEDABAD_TEX", "DC_DELHI", "rail"),
    ("SUP_PUNE_ELEC", "DC_PUNE", "road"),
    ("SUP_PUNE_ELEC", "DC_HYD_MEDCHAL", "road"),
    # DC <-> DC transfers
    ("DC_BLR", "DC_HYD_SHAMSHABAD", "road"),
    ("DC_HYD_MEDCHAL", "DC_NAGPUR", "road"),
    ("DC_NAGPUR", "DC_DELHI", "rail"),
    ("DC_PUNE", "DC_HYD_MEDCHAL", "road"),
    ("DC_BLR", "DC_HYD_SHAMSHABAD", "air"),
    # DC -> demand zones (last mile / regional distribution)
    ("DC_HYD_MEDCHAL", "Z_HYD", "road"),
    ("DC_HYD_SHAMSHABAD", "Z_HYD", "road"),
    ("DC_HYD_SHAMSHABAD", "Z_VJA", "road"),
    ("DC_HYD_MEDCHAL", "Z_VSK", "road"),
    ("DC_BLR", "Z_BLR", "road"),
    ("DC_BLR", "Z_CHN", "road"),
    ("DC_BLR", "Z_CBE", "road"),
    ("DC_PUNE", "Z_PUN", "road"),
    ("DC_PUNE", "Z_MUM", "road"),
    ("DC_DELHI", "Z_DEL", "road"),
    ("DC_DELHI", "Z_JAI", "road"),
    ("DC_NAGPUR", "Z_KOL", "rail"),
    ("DC_DELHI", "Z_AMD", "rail"),
    # Phase 2 additions: cold-chain coverage for every zone + FMCG reach to the south
    ("DC_HYD_SHAMSHABAD", "Z_VSK", "road"),
    ("DC_DELHI", "Z_KOL", "road"),
    ("SUP_AHMEDABAD_TEX", "DC_NAGPUR", "rail"),
    ("DC_NAGPUR", "DC_HYD_MEDCHAL", "road"),
    ("DC_HYD_MEDCHAL", "DC_HYD_SHAMSHABAD", "road"),
    ("SUP_AHMEDABAD_TEX", "DC_BLR", "rail"),
    ("PLANT_PATANCHERU", "DC_PUNE", "road"),
]

# mode: detour, cost ₹ per unit-km, CO2 kg per tonne-km, effective speed km/h, lognormal sigma, capacity units/day
MODE = {
    "road": dict(detour=1.30, cost_km=0.060, co2=0.062, speed=38.0, sigma=0.25, cap=6000, fixed_h=4),
    "rail": dict(detour=1.30, cost_km=0.030, co2=0.022, speed=28.0, sigma=0.35, cap=15000, fixed_h=18),
    "sea": dict(detour=1.00, cost_km=0.008, co2=0.012, speed=26.0, sigma=0.30, cap=40000, fixed_h=72),
    "air": dict(detour=1.05, cost_km=0.450, co2=0.600, speed=650.0, sigma=0.15, cap=800, fixed_h=6),
}

# Sea legs follow real shipping routes rather than crossing land.
SEA_WAYPOINTS = {
    "east": [(22.2, 114.3), (12.0, 111.0), (1.25, 104.0), (3.2, 100.6), (6.0, 94.6)],   # S. China Sea -> Singapore -> Malacca -> Bay of Bengal
    "west_extra": [(5.7, 80.6), (7.6, 77.2), (15.0, 72.6)],                              # Dondra Head -> Cape Comorin -> Arabian Sea
}

SKUS = [
    {"id": "SKU_VAX", "family": "vaccine", "name": "Vaccines (2–8 °C)", "unit": "box of 100 doses",
     "unit_value": 1850, "unit_weight_kg": 2.5, "perishable": True, "shelf_life_h": 720, "cold_chain": True,
     "sea_imported": False, "base_demand_per_million_day": 9.0, "holding_rate_yr": 0.30, "stockout_penalty": 900},
    {"id": "SKU_FMCG", "family": "fmcg", "name": "FMCG staples", "unit": "case",
     "unit_value": 120, "unit_weight_kg": 9.0, "perishable": False, "shelf_life_h": None, "cold_chain": False,
     "sea_imported": False, "base_demand_per_million_day": 140.0, "holding_rate_yr": 0.20, "stockout_penalty": 40},
    {"id": "SKU_ELEC", "family": "electronics", "name": "Consumer electronics", "unit": "carton",
     "unit_value": 9400, "unit_weight_kg": 6.0, "perishable": False, "shelf_life_h": None, "cold_chain": False,
     "sea_imported": True, "base_demand_per_million_day": 12.0, "holding_rate_yr": 0.25, "stockout_penalty": 1500},
]


def haversine_km(a: tuple[float, float], b: tuple[float, float]) -> float:
    lat1, lon1, lat2, lon2 = map(math.radians, (a[0], a[1], b[0], b[1]))
    h = math.sin((lat2 - lat1) / 2) ** 2 + math.cos(lat1) * math.cos(lat2) * math.sin((lon2 - lon1) / 2) ** 2
    return 2 * 6371.0 * math.asin(math.sqrt(h))


def sea_path(src: tuple[float, float], dst: tuple[float, float]) -> list[tuple[float, float]]:
    pts = [src, *SEA_WAYPOINTS["east"]]
    if dst[1] < 78.0:  # west-coast port: round Sri Lanka and the southern tip of India
        pts += SEA_WAYPOINTS["west_extra"]
    return [*pts, dst]


def path_km(pts: list[tuple[float, float]]) -> float:
    return sum(haversine_km(pts[i], pts[i + 1]) for i in range(len(pts) - 1))


def osrm_road(a: tuple[float, float], b: tuple[float, float]) -> tuple[float, float] | None:
    url = f"https://router.project-osrm.org/route/v1/driving/{a[1]},{a[0]};{b[1]},{b[0]}?overview=false"
    try:
        with urllib.request.urlopen(url, timeout=25) as r:
            route = json.load(r)["routes"][0]
        return route["distance"] / 1000.0, route["duration"] / 3600.0
    except Exception as e:  # network down, rate limit: fall back to haversine x detour
        print(f"  OSRM failed for {a}->{b}: {e}")
        return None


def build(refresh: bool) -> None:
    by_id = {n[0]: n for n in NODES}
    cache: dict[str, list[float]] = json.loads(OSRM_CACHE.read_text()) if OSRM_CACHE.exists() and not refresh else {}

    nodes = [{"id": i, "type": t, "name": nm, "lat": la, "lon": lo, "capacity": cap, "attrs": at, "status": "ok"}
             for i, t, nm, la, lo, cap, at in NODES]

    lanes = []
    for k, (a, b, mode) in enumerate(LANE_DEFS, 1):
        m = MODE[mode]
        pa, pb = (by_id[a][3], by_id[a][4]), (by_id[b][3], by_id[b][4])
        source = "haversine x %.2f" % m["detour"]
        if mode == "road":
            key = f"{a}->{b}"
            if key not in cache:
                res = osrm_road(pa, pb)
                if res:
                    cache[key] = [round(res[0], 1), round(res[1], 2)]
            if key in cache:
                d, source = cache[key][0], "OSRM"
            else:
                d = haversine_km(pa, pb) * m["detour"]
        elif mode == "sea":
            d, source = path_km(sea_path(pa, pb)), "sea waypoints"
        else:
            d = haversine_km(pa, pb) * m["detour"]
        mean_h = d / m["speed"] + m["fixed_h"]
        sigma = m["sigma"]
        mu = math.log(mean_h) - sigma**2 / 2  # lognormal with E[T] = mean_h
        lanes.append({
            "id": f"L{k:03d}", "from_id": a, "to_id": b, "mode": mode,
            "distance_km": round(d, 1), "distance_source": source,
            "cost_per_unit_km": m["cost_km"], "cost_per_unit": round(m["cost_km"] * d, 2),
            "capacity": m["cap"], "co2_per_tkm": m["co2"],
            "lt_mu": round(mu, 4), "lt_sigma": sigma, "lt_mean_h": round(mean_h, 1),
            "lt_p50_h": round(math.exp(mu), 1), "lt_p90_h": round(math.exp(mu + 1.2816 * sigma), 1),
            "status": "ok",
        })
    OSRM_CACHE.write_text(json.dumps(dict(sorted(cache.items())), indent=1) + "\n")

    sourcing = build_sourcing(nodes, lanes)
    for name, obj in (("nodes", nodes), ("lanes", lanes), ("skus", SKUS), ("sourcing", sourcing)):
        (OUT / f"{name}.json").write_text(json.dumps(obj, indent=2, ensure_ascii=False) + "\n")
    print(f"{len(nodes)} nodes, {len(lanes)} lanes ({sum(l['distance_source'] == 'OSRM' for l in lanes)} OSRM road), "
          f"{len(SKUS)} skus, {len(sourcing['replenishment'])} DC x SKU sourcing paths, {len(sourcing['serving'])} zone x SKU assignments")


def stocks(dc: dict, sku: dict) -> bool:
    return (not sku["cold_chain"]) or bool(dc["attrs"].get("cold_chain"))


def build_sourcing(nodes: list[dict], lanes: list[dict]) -> dict:
    """Primary + alternate replenishment paths per DC x SKU, and the serving DC per zone x SKU.

    Rules: a path may pass through ports and other DCs but never a zone; vaccine paths may only
    touch cold-chain DCs; electronics prefer the overseas (sea-imported) source, the rest pick the
    fastest source. Up to 3 alternates are kept for rerouting (used by the optimiser in Phase 7).
    """
    nid = {n["id"]: n for n in nodes}
    G = nx.MultiDiGraph()
    for l in lanes:
        G.add_edge(l["from_id"], l["to_id"], key=l["id"], w=l["lt_mean_h"])
    replenishment = []
    for dc in (n for n in nodes if n["type"] == "dc"):
        for sku in SKUS:
            if not stocks(dc, sku):
                continue
            ok_nodes = {n["id"] for n in nodes if n["type"] in ("port", "plant", "supplier")
                        or (n["type"] == "dc" and stocks(n, sku))}
            H = nx.DiGraph()
            for u, v, k, d in G.edges(keys=True, data=True):
                if u in ok_nodes and v in ok_nodes and (not H.has_edge(u, v) or H[u][v]["w"] > d["w"]):
                    H.add_edge(u, v, w=d["w"], lane=k)
            cands = []
            for src in (n for n in nodes if sku["id"] in n["attrs"].get("produces", [])):
                if src["id"] not in H or dc["id"] not in H or not nx.has_path(H, src["id"], dc["id"]):
                    continue
                for n_paths, p in enumerate(nx.shortest_simple_paths(H, src["id"], dc["id"], weight="w")):
                    if n_paths >= 4:
                        break
                    lane_ids = [H[p[i]][p[i + 1]]["lane"] for i in range(len(p) - 1)]
                    cands.append({"source": src["id"], "nodes": p, "lanes": lane_ids,
                                  "lead_mean_h": round(sum(H[p[i]][p[i + 1]]["w"] for i in range(len(p) - 1)), 1),
                                  "overseas": bool(src["attrs"].get("overseas"))})
            if not cands:
                raise SystemExit(f"no sourcing path for {dc['id']} x {sku['id']}")
            key = (lambda c: (not c["overseas"], c["lead_mean_h"])) if sku["sea_imported"] else (lambda c: c["lead_mean_h"])
            cands.sort(key=key)
            replenishment.append({"dc": dc["id"], "sku": sku["id"], "primary": cands[0], "alternates": cands[1:4]})

    serving = []
    for z in (n for n in nodes if n["type"] == "zone"):
        for sku in SKUS:
            opts = [(l, nid[l["from_id"]]) for l in lanes if l["to_id"] == z["id"] and stocks(nid[l["from_id"]], sku)]
            if not opts:
                raise SystemExit(f"zone {z['id']} cannot be served {sku['id']}")
            opts.sort(key=lambda o: (sku["family"] not in o[1]["attrs"].get("primary", [sku["family"]]), o[0]["lt_mean_h"]))
            serving.append({"zone": z["id"], "sku": sku["id"], "dc": opts[0][1]["id"], "lane": opts[0][0]["id"],
                            "backup": [{"dc": o[1]["id"], "lane": o[0]["id"]} for o in opts[1:]]})
    return {"replenishment": replenishment, "serving": serving}


if __name__ == "__main__":
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--refresh", action="store_true", help="re-query OSRM for every road lane")
    build(ap.parse_args().refresh)
