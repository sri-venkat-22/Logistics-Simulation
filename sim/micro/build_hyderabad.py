"""Build the SUMO micro-twin of the Hyderabad logistics belt (western + southern belt).

    .venv/bin/python -m sim.micro.build_hyderabad            # build from cached OSM tiles
    .venv/bin/python -m sim.micro.build_hyderabad --download # (re)fetch OSM via osmGet.py first

Steps
 1. OSM   bbox 78.22,17.15,78.53,17.66 (Medchal - ORR - Gachibowli - Shamshabad - Patancheru,
          not the whole ORR), road types motorway..tertiary only. Source: the OpenStreetMap France
          Telangana extract (md5-verified), clipped + filtered with pyosmium — one consistent
          snapshot. Fallback: osmGet.py against Overpass (often 504s on dense central Hyderabad).
 2. net   netconvert --keep-edges.by-type motorway..tertiary(+links) --geometry.remove
          --ramps.guess --junctions.join (+ TLS guessing, largest connected component).
 3. types truck vType (vClass=truck, maxSpeed=22 m/s, length=12 m) and passenger car vType.
 4. hubs  DC / plant / industrial-area locations snapped to the nearest truck-accessible edge,
          each with a parkingArea (hubs.json maps hub name -> edge / parkingArea id).
 5. cars  background traffic from randomTrips.py (fringe-weighted, >= 3 km trips).
 6. cfg   hyderabad.sumocfg (net + vtypes + parking + background cars + demo trucks).
"""
from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys
from pathlib import Path

import sumolib

from sim.paths import HYDERABAD

BBOX = (78.22, 17.15, 78.53, 17.66)  # west, south, east, north
ROAD_TYPES = ["motorway", "motorway_link", "trunk", "trunk_link", "primary", "primary_link",
              "secondary", "secondary_link", "tertiary", "tertiary_link"]
OSM_DIR = HYDERABAD / "osm"
EXTRACT_URL = "https://download.openstreetmap.fr/extracts/asia/india/telangana.osm.pbf"
PBF = OSM_DIR / "telangana.osm.pbf"
BELT = OSM_DIR / "hyderabad_belt.osm.xml"
NET = HYDERABAD / "hyderabad.net.xml.gz"

# name, lat, lon, kind — DCs and plants from data/nodes.json plus industrial / consumption hubs
HUBS = [
    ("Medchal DC", 17.630, 78.480, "dc"),
    ("Shamshabad DC", 17.240, 78.430, "dc"),
    ("Patancheru Plant", 17.530, 78.260, "plant"),
    ("Kothur Pharma", 17.160, 78.290, "plant"),
    ("Jeedimetla Industrial", 17.515, 78.455, "industrial"),
    ("Kompally", 17.536, 78.487, "consumer"),
    ("Kukatpally", 17.494, 78.399, "consumer"),
    ("Gachibowli", 17.440, 78.348, "consumer"),
    ("Hitec City", 17.447, 78.376, "consumer"),
    ("Mehdipatnam", 17.395, 78.437, "consumer"),
    ("Secunderabad", 17.439, 78.498, "consumer"),
    ("RGIA Air Cargo", 17.232, 78.448, "airport"),
]


def sumo_home() -> Path:
    import sumo  # the eclipse-sumo wheel
    return Path(sumo.SUMO_HOME)


def run(cmd: list[str]) -> None:
    print("  $", " ".join(str(c) for c in cmd[:6]), "..." if len(cmd) > 6 else "")
    subprocess.run([str(c) for c in cmd], check=True, env={**os.environ, "SUMO_HOME": str(sumo_home())})


def download(overpass: bool = False) -> None:
    OSM_DIR.mkdir(parents=True, exist_ok=True)
    if overpass:
        run([sys.executable, sumo_home() / "tools" / "osmGet.py", "-b", ",".join(map(str, BBOX)), "-t", "3",
             "-u", "https://overpass.kumi.systems/api/interpreter,pcoffee,oapi,hpi",
             "-r", json.dumps({"highway": ROAD_TYPES}), "-p", "hyderabad", "-d", OSM_DIR, "--retries", "4"])
        return
    import hashlib
    import urllib.request
    for url, dest in ((EXTRACT_URL, PBF), (EXTRACT_URL + ".md5", PBF.with_suffix(".pbf.md5"))):
        print(f"  downloading {url}")
        urllib.request.urlretrieve(url, dest)
    want = PBF.with_suffix(".pbf.md5").read_text().split()[0]
    got = hashlib.md5(PBF.read_bytes()).hexdigest()
    if want != got:
        raise SystemExit(f"md5 mismatch for {PBF.name}: {got} != {want}")


def clip_extract() -> Path:
    """Keep highway ways of ROAD_TYPES with at least one node in BBOX, plus all their nodes."""
    import osmium

    w, s, e, n = BBOX
    types = set(ROAD_TYPES)

    class Ways(osmium.SimpleHandler):
        def __init__(self):
            super().__init__()
            self.ways, self.nodes = set(), set()

        def way(self, way):
            if way.tags.get("highway") not in types:
                return
            inside = False
            for nd in way.nodes:
                if nd.location.valid() and w <= nd.location.lon <= e and s <= nd.location.lat <= n:
                    inside = True
                    break
            if inside:
                self.ways.add(way.id)
                self.nodes.update(nd.ref for nd in way.nodes)

    h = Ways()
    h.apply_file(str(PBF), locations=True)
    BELT.unlink(missing_ok=True)
    writer = osmium.SimpleWriter(str(BELT))

    class Out(osmium.SimpleHandler):
        def node(self, node):
            if node.id in h.nodes:
                writer.add_node(node)

        def way(self, way):
            if way.id in h.ways:
                writer.add_way(way)

    Out().apply_file(str(PBF))
    writer.close()
    print(f"  clipped {PBF.name} -> {BELT.name}: {len(h.ways):,} ways, {len(h.nodes):,} nodes")
    return BELT


def netconvert() -> None:
    if PBF.exists() and not BELT.exists():
        clip_extract()
    tiles = [BELT] if BELT.exists() else sorted(OSM_DIR.glob("hyderabad*_*.osm.xml"))
    if not tiles:
        raise SystemExit("no OSM data in sim/micro/hyderabad/osm — run with --download")
    keep = ",".join(f"highway.{t}" for t in ROAD_TYPES)
    run([sumo_home() / "bin" / "netconvert", "--osm-files", ",".join(map(str, tiles)),
         "--keep-edges.by-type", keep,
         "--geometry.remove", "--ramps.guess", "--junctions.join",
         "--roundabouts.guess", "--tls.guess-signals", "--tls.discard-simple", "--tls.join",
         "--keep-edges.in-geo-boundary", ",".join(map(str, BBOX)),
         "--remove-edges.isolated", "--keep-edges.components", "1",
         "--output.street-names", "--osm.oneway-spread-right",
         "--default.lanewidth", "3.3", "--no-warnings",
         "-o", NET])


def write_vtypes() -> Path:
    p = HYDERABAD / "vtypes.add.xml"
    p.write_text("""<?xml version="1.0" encoding="UTF-8"?>
<!-- AEGIS micro-twin vehicle types -->
<additional>
    <!-- freight truck: 12 m rigid/semi, 22 m/s (79 km/h) cap -->
    <vType id="truck" vClass="truck" maxSpeed="22" length="12" minGap="3" accel="1.0" decel="4.0"
           sigma="0.5" speedFactor="0.95" speedDev="0.05" guiShape="truck" color="34,211,238"/>
    <vType id="car" vClass="passenger" length="4.5" accel="2.6" decel="4.5" sigma="0.5"
           speedFactor="1.0" speedDev="0.1" color="154,167,189"/>
</additional>
""")
    return p


def write_hubs(net: sumolib.net.Net) -> dict:
    hubs, parks = {}, []
    for name, lat, lon, kind in HUBS:
        x, y = net.convertLonLat2XY(lon, lat)
        cands = []
        for r in (150, 400, 1000, 2500):
            cands = [(e, d) for e, d in net.getNeighboringEdges(x, y, r)
                     if e.allows("truck") and e.getLength() >= 60 and e.getFunction() == ""]
            if cands:
                break
        if not cands:
            print(f"  ! no truck edge near {name}")
            continue
        edge, dist = min(cands, key=lambda c: c[1])
        lane = edge.getLanes()[0]
        L = lane.getLength()
        start = max(5.0, L / 2 - 25)
        pid = "pa_" + name.lower().replace(" ", "_")
        parks.append(f'    <parkingArea id="{pid}" lane="{lane.getID()}" startPos="{start:.1f}" endPos="{min(L - 5, start + 50):.1f}" '
                     f'roadsideCapacity="12" name="{name}"/>')
        hubs[name] = {"kind": kind, "lat": lat, "lon": lon, "edge": edge.getID(), "lane": lane.getID(),
                      "parking_area": pid, "snap_m": round(dist, 1), "street": edge.getName()}
    (HYDERABAD / "parking.add.xml").write_text('<?xml version="1.0" encoding="UTF-8"?>\n<additional>\n'
                                                + "\n".join(parks) + "\n</additional>\n")
    (HYDERABAD / "hubs.json").write_text(json.dumps(hubs, indent=2) + "\n")
    return hubs


def background_cars(end_s: int = 3600, period: float = 0.5, seed: int = 42) -> Path:
    out = HYDERABAD / "cars.rou.xml"
    run([sys.executable, sumo_home() / "tools" / "randomTrips.py", "-n", NET, "-o", HYDERABAD / "cars.trips.xml",
         "-r", out, "-b", "0", "-e", str(end_s), "-p", str(period), "--fringe-factor", "5",
         "--min-distance", "3000", "--seed", str(seed), "--prefix", "car", "--edge-permission", "passenger",
         "--trip-attributes", 'type="car" departLane="best" departSpeed="max"',
         "--additional-files", HYDERABAD / "vtypes.add.xml", "--validate", "--remove-loops"])
    (HYDERABAD / "cars.trips.xml").unlink(missing_ok=True)
    import re
    text = out.read_text()
    text = re.sub(r"<!--.*?-->\s*", "", text, flags=re.S)  # generator headers embed absolute local paths
    text = re.sub(r"\s*<vType\b.*?(/>|</vType>)", "", text, flags=re.S)  # vtypes come from vtypes.add.xml
    out.write_text(text)
    return out


def demo_trucks(hubs: dict, n: int = 60, seed: int = 7) -> Path:
    import random
    rnd = random.Random(seed)
    names = [h for h, v in hubs.items() if v["kind"] in ("dc", "plant", "airport", "industrial")]
    dests = list(hubs)
    rows = []
    for i in range(n):
        a = rnd.choice(names)
        b = rnd.choice([d for d in dests if d != a])
        rows.append(f'    <trip id="truck{i:03d}" type="truck" depart="{i * 30}" from="{hubs[a]["edge"]}" '
                    f'to="{hubs[b]["edge"]}" departLane="best">\n'
                    f'        <stop parkingArea="{hubs[b]["parking_area"]}" duration="600"/>\n    </trip>')
    p = HYDERABAD / "trucks.rou.xml"
    p.write_text('<?xml version="1.0" encoding="UTF-8"?>\n<routes>\n' + "\n".join(rows) + "\n</routes>\n")
    return p


FLOOD_POINT = (17.2828, 78.3828)  # ORR between Shamshabad and Gachibowli (Narsingi side)


def update_flood_template(net: sumolib.net.Net, radius_m: float = 1500) -> list[str]:
    """Put the real ORR edge ids around FLOOD_POINT into the road_flood scenario template."""
    from sim.paths import SCENARIO_TEMPLATES
    x, y = net.convertLonLat2XY(FLOOD_POINT[1], FLOOD_POINT[0])
    edges = sorted({e.getID() for e, _ in net.getNeighboringEdges(x, y, radius_m)
                    if "outer ring road" in (e.getName() or "").lower() and e.getFunction() == ""})
    path = SCENARIO_TEMPLATES / "road_flood.json"
    tpl = json.loads(path.read_text())
    tpl["params"]["sumo_edges"] = edges
    path.write_text(json.dumps(tpl, indent=2, ensure_ascii=False) + "\n")
    return edges


def write_cfg() -> Path:
    p = HYDERABAD / "hyderabad.sumocfg"
    p.write_text("""<?xml version="1.0" encoding="UTF-8"?>
<configuration>
    <input>
        <net-file value="hyderabad.net.xml.gz"/>
        <route-files value="cars.rou.xml,trucks.rou.xml"/>
        <additional-files value="vtypes.add.xml,parking.add.xml"/>
    </input>
    <time>
        <begin value="0"/>
        <step-length value="1"/>
    </time>
    <processing>
        <time-to-teleport value="300"/>
        <ignore-route-errors value="true"/>
        <device.rerouting.adaptation-interval value="60"/>
    </processing>
    <routing>
        <device.rerouting.probability value="0.2"/>
        <device.rerouting.period value="300"/>
    </routing>
    <report>
        <no-step-log value="true"/>
        <no-warnings value="true"/>
    </report>
</configuration>
""")
    return p


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--download", action="store_true", help="fetch the Telangana OSM extract first")
    ap.add_argument("--overpass", action="store_true", help="with --download: use osmGet.py/Overpass instead")
    a = ap.parse_args()
    if a.download:
        download(a.overpass)
        BELT.unlink(missing_ok=True)
    netconvert()
    net = sumolib.net.readNet(str(NET))
    xmin, ymin, xmax, ymax = net.getBoundary()
    print(f"  net: {len(net.getEdges()):,} edges, {len(net.getNodes()):,} junctions, "
          f"{(xmax - xmin) / 1000:.1f} x {(ymax - ymin) / 1000:.1f} km, "
          f"{sum(e.getLength() * e.getLaneNumber() for e in net.getEdges()) / 1000:,.0f} lane-km")
    write_vtypes()
    hubs = write_hubs(net)
    print(f"  hubs: {len(hubs)} parkingAreas ({', '.join(hubs)})")
    flood = update_flood_template(net)
    print(f"  road_flood template: {len(flood)} ORR edges near {FLOOD_POINT}")
    background_cars()
    demo_trucks(hubs)
    write_cfg()
    print(f"  wrote {HYDERABAD.relative_to(HYDERABAD.parents[2])}/hyderabad.sumocfg")


if __name__ == "__main__":
    main()
