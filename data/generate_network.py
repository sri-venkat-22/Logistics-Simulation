"""Generate the AEGIS Twin reference network (nodes, lanes, SKUs).

Deterministic: same output on every run. Coordinates are real (approximate site
locations); lane parameters are engineering estimates to be calibrated in Phase 2/3.

    python3 data/generate_network.py
"""
from __future__ import annotations

import json
import math
from pathlib import Path

OUT = Path(__file__).parent

# id, type, name, lat, lon, capacity (units/day), attrs
NODES = [
    # Ports
    ("PORT_JNPT", "port", "JNPT / Nhava Sheva", 18.950, 72.950, 60000, {"berths": 6}),
    ("PORT_MUNDRA", "port", "Mundra Port", 22.740, 69.700, 55000, {"berths": 5}),
    ("PORT_CHENNAI", "port", "Chennai Port", 13.100, 80.300, 40000, {"berths": 4}),
    ("PORT_VIZAG", "port", "Visakhapatnam Port", 17.690, 83.280, 30000, {"berths": 3}),
    ("PORT_KOLKATA", "port", "Kolkata / Haldia", 22.030, 88.060, 28000, {"berths": 3}),
    # Plants / suppliers
    ("PLANT_PATANCHERU", "plant", "Patancheru Pharma API Plant", 17.530, 78.260, 12000, {"family": "vaccine"}),
    ("SUP_CHENNAI_AUTO", "supplier", "Sriperumbudur Auto Parts", 12.960, 79.950, 9000, {"family": "electronics"}),
    ("SUP_AHMEDABAD_TEX", "supplier", "Ahmedabad Textiles", 23.020, 72.570, 15000, {"family": "fmcg"}),
    ("SUP_PUNE_ELEC", "supplier", "Chakan Electronics", 18.760, 73.860, 8000, {"family": "electronics"}),
    ("SUP_SHENZHEN", "supplier", "Shenzhen Electronics (overseas)", 22.540, 114.060, 20000, {"family": "electronics"}),
    # Distribution centres
    ("DC_HYD_MEDCHAL", "dc", "Hyderabad-Medchal DC (FMCG/e-com)", 17.630, 78.480, 18000, {"cold_chain": False}),
    ("DC_HYD_SHAMSHABAD", "dc", "Hyderabad-Shamshabad DC (pharma cold chain)", 17.240, 78.430, 9000, {"cold_chain": True}),
    ("DC_BLR", "dc", "Bengaluru-Hoskote DC", 13.070, 77.800, 16000, {"cold_chain": True}),
    ("DC_NAGPUR", "dc", "Nagpur Central Hub", 21.100, 79.050, 22000, {"cold_chain": False}),
    ("DC_DELHI", "dc", "Delhi-NCR (Gurugram) DC", 28.460, 77.030, 24000, {"cold_chain": True}),
    ("DC_PUNE", "dc", "Pune-Chakan DC", 18.700, 73.800, 14000, {"cold_chain": False}),
    # Demand zones
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

# from, to, mode
LANE_DEFS = [
    # inbound sea
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
    # plants/suppliers -> DC
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
    # DC -> demand zones
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
]

# mode: (detour, cost ₹/unit-km, co2 kg/t-km, speed km/h, sigma, capacity/day)
MODE = {
    "road": (1.30, 0.060, 0.062, 38.0, 0.25, 6000),
    "rail": (1.25, 0.030, 0.022, 28.0, 0.35, 15000),
    "sea": (1.15, 0.008, 0.012, 26.0, 0.30, 40000),
    "air": (1.05, 0.450, 0.600, 650.0, 0.15, 800),
}
FIXED_H = {"road": 4, "rail": 18, "sea": 72, "air": 6}  # handling / dwell


def haversine_km(a: tuple[float, float], b: tuple[float, float]) -> float:
    lat1, lon1, lat2, lon2 = map(math.radians, (a[0], a[1], b[0], b[1]))
    h = math.sin((lat2 - lat1) / 2) ** 2 + math.cos(lat1) * math.cos(lat2) * math.sin((lon2 - lon1) / 2) ** 2
    return 2 * 6371.0 * math.asin(math.sqrt(h))


def main() -> None:
    by_id = {n[0]: n for n in NODES}
    nodes = [
        {"id": i, "type": t, "name": nm, "lat": la, "lon": lo, "capacity": cap, "attrs": at, "status": "ok"}
        for i, t, nm, la, lo, cap, at in NODES
    ]
    lanes = []
    for k, (a, b, mode) in enumerate(LANE_DEFS, 1):
        detour, cost, co2, speed, sigma, cap = MODE[mode]
        d = haversine_km(by_id[a][3:5], by_id[b][3:5]) * detour
        mean_h = d / speed + FIXED_H[mode]
        # lognormal params such that E[T] = mean_h
        mu = math.log(mean_h) - sigma**2 / 2
        lanes.append({
            "id": f"L{k:03d}", "from_id": a, "to_id": b, "mode": mode,
            "distance_km": round(d, 1), "cost_per_unit": round(cost * d, 2), "capacity": cap,
            "co2_per_tkm": co2, "lt_mu": round(mu, 4), "lt_sigma": sigma,
            "lt_mean_h": round(mean_h, 1), "status": "ok",
        })
    skus = [
        {"id": "SKU_VAX", "family": "vaccine", "name": "Vaccines (2–8 °C)", "unit_value": 1850, "perishable": True, "shelf_life_h": 720, "cold_chain": True},
        {"id": "SKU_FMCG", "family": "fmcg", "name": "FMCG staples", "unit_value": 120, "perishable": False, "shelf_life_h": None, "cold_chain": False},
        {"id": "SKU_ELEC", "family": "electronics", "name": "Consumer electronics", "unit_value": 9400, "perishable": False, "shelf_life_h": None, "cold_chain": False},
    ]
    for name, obj in (("nodes", nodes), ("lanes", lanes), ("skus", skus)):
        (OUT / f"{name}.json").write_text(json.dumps(obj, indent=2) + "\n")
    print(f"{len(nodes)} nodes, {len(lanes)} lanes, {len(skus)} skus")


if __name__ == "__main__":
    main()
