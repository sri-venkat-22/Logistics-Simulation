"""Phase 2 network dataset: real coordinates, required nodes, lane parameters, SKUs, sourcing."""
import math

from sim.macro.network import Network


def test_required_nodes(net: Network):
    by_type = {t: {n.id for n in net.of_type(t)} for t in ("port", "plant", "supplier", "dc", "zone")}
    assert by_type["port"] == {"PORT_JNPT", "PORT_MUNDRA", "PORT_CHENNAI", "PORT_VIZAG", "PORT_KOLKATA"}
    assert "PLANT_PATANCHERU" in by_type["plant"]
    assert {"SUP_CHENNAI_AUTO", "SUP_AHMEDABAD_TEX", "SUP_PUNE_ELEC", "SUP_SHENZHEN"} <= by_type["supplier"]
    assert by_type["dc"] == {"DC_HYD_MEDCHAL", "DC_HYD_SHAMSHABAD", "DC_BLR", "DC_NAGPUR", "DC_DELHI", "DC_PUNE"}
    assert 10 <= len(by_type["zone"]) <= 14
    assert net.nodes["DC_HYD_SHAMSHABAD"].cold_chain and not net.nodes["DC_HYD_MEDCHAL"].cold_chain
    assert net.nodes["SUP_SHENZHEN"].attrs.get("overseas")


def test_real_coordinates(net: Network):
    for n in net.nodes.values():
        if n.id == "SUP_SHENZHEN":
            assert 22 < n.lat < 23 and 113.5 < n.lon < 114.5
        else:
            assert 6 < n.lat < 36 and 68 < n.lon < 98, f"{n.id} is outside India"
    # spot checks against known locations (~within 25 km)
    for nid, (lat, lon) in {"PORT_CHENNAI": (13.10, 80.29), "PORT_JNPT": (18.95, 72.95), "DC_HYD_SHAMSHABAD": (17.24, 78.43)}.items():
        n = net.nodes[nid]
        assert math.hypot(n.lat - lat, n.lon - lon) < 0.25


def test_lanes(net: Network):
    modes = {l.mode for l in net.lanes.values()}
    assert modes == {"road", "rail", "sea", "air"}
    for l in net.lanes.values():
        assert l.distance_km > 0 and l.capacity > 0 and l.co2_per_tkm > 0
        assert math.isclose(l.cost_per_unit, l.cost_per_unit_km * l.distance_km, rel_tol=0.02)
        assert abs(math.exp(l.lt_mu + l.lt_sigma**2 / 2) - l.lt_mean_h) < 0.05 * l.lt_mean_h + 0.1
        assert l.lt_p90_h > math.exp(l.lt_mu)
    sea = [l for l in net.lanes.values() if l.mode == "sea"]
    assert all(l.from_id == "SUP_SHENZHEN" for l in sea)
    # sea legs go round via Malacca, so they are far longer than the straight line
    chn = net.lanes["L001"]
    assert chn.to_id == "PORT_CHENNAI" and chn.distance_km > 5000
    # mode economics: air is the most expensive and dirtiest per unit-km, sea the cheapest
    rate = {m: min(l.cost_per_unit_km for l in net.lanes.values() if l.mode == m) for m in modes}
    assert rate["sea"] < rate["rail"] < rate["road"] < rate["air"]


def test_skus(net: Network):
    fam = {s.family: s for s in net.skus.values()}
    assert set(fam) == {"vaccine", "fmcg", "electronics"}
    assert fam["vaccine"].cold_chain and fam["vaccine"].unit_value > fam["fmcg"].unit_value
    assert fam["fmcg"].base_demand_per_million_day == max(s.base_demand_per_million_day for s in net.skus.values())
    assert fam["electronics"].sea_imported and fam["electronics"].unit_value > 1000


def test_sourcing(net: Network):
    assert not net.stocks("DC_HYD_MEDCHAL", "SKU_VAX")  # no cold chain
    for (dc, sku), paths in net.replenishment.items():
        p = paths[0]
        assert p.nodes[-1] == dc and "SKU_" in sku
        if net.skus[sku].sea_imported:
            assert p.source == "SUP_SHENZHEN", f"{dc} electronics should be sea-imported"
    assert net.serving[("Z_HYD", "SKU_VAX")][0] == "DC_HYD_SHAMSHABAD"
    assert net.serving[("Z_HYD", "SKU_FMCG")][0] == "DC_HYD_MEDCHAL"
