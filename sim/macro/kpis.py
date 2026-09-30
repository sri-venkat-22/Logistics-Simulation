"""KPIs of a macro-twin run, and the event-log rows that evidence each one.

    fill rate        units filled from stock at order time / units demanded
    OTIF             orders due in the window delivered in full (no backorder) by the promised time
    backorders       units still backordered at the end (and units ever backordered / lost)
    inventory days   time-averaged on-hand stock / average daily demand
    cost             transport (booked at dispatch of each leg) + holding (hourly) + stock-out penalty
                     (+ fixed ordering cost when SKUs define one)
    CO2              t, booked at dispatch of each leg
    TTS / TTR        per disruption and per node (Simchi-Levi): TTR = time until the node is back at full
                     capacity; TTS = time from the disruption until the first stock-out at a DC x SKU that
                     depends on it (None = survived the window, a censored lower bound); exposed = TTS < TTR
"""
from __future__ import annotations

import math
from datetime import timedelta
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from sim.macro.engine import Twin

EVIDENCE: dict[str, tuple[str, ...]] = {
    "fill_rate": ("order", "backorder", "lost_sale"),
    "otif": ("order", "order_complete"),
    "backorders": ("backorder", "stockout_start", "stockout_end"),
    "inventory_days": ("inventory", "receipt"),
    "cost_transport": ("depart",),
    "cost_holding": ("inventory",),
    "cost_penalty": ("backorder", "lost_sale"),
    "cost_ordering": ("replenish",),
    "co2": ("depart",),
    "tts_ttr": ("disruption_start", "disruption_end", "stockout_start", "stockout_end"),
}


def disruption_metrics(twin: "Twin") -> list[dict]:
    out = []
    now = twin.env.now
    for e in twin.effect_history:
        deps = twin.dependents(e)
        eps = [ep for ep in twin.stockouts if (ep["dc"], ep["sku"]) in deps and ep["start_h"] >= e.start_h - 1e-9]
        active = e in twin.effects
        ttr = None if active and math.isinf(e.end_h) else (min(e.end_h, now) if active else e.end_h) - e.start_h
        tts = min(ep["start_h"] for ep in eps) - e.start_h if eps else None
        if eps and all(ep["end_h"] is not None for ep in eps) and not active:
            restored = max(e.end_h, max(ep["end_h"] for ep in eps)) - e.start_h
        elif not eps and not active:
            restored = e.end_h - e.start_h
        else:
            restored = None
        s = e.summary()
        s.update({
            "ts": (twin.start + timedelta(hours=e.start_h)).isoformat(timespec="minutes"),
            "active": active, "ttr_h": None if ttr is None else round(ttr, 2),
            "tts_h": None if tts is None else round(tts, 2),
            "survived_h": round(now - e.start_h, 2) if tts is None else None,
            "service_restored_h": None if restored is None else round(restored, 2),
            "exposed": tts is not None and (ttr is None or tts < ttr),
            "stockouts": sorted({f"{ep['dc']}/{ep['sku']}" for ep in eps}),
            "dependents": len(deps),
        })
        out.append(s)
    return out


def node_tts_ttr(disruptions: list[dict]) -> dict[str, dict]:
    """Per node: longest TTR and shortest TTS over the disruptions that degraded it."""
    table: dict[str, dict] = {}
    for d in disruptions:
        for n in d["nodes"]:
            t = table.setdefault(n, {"ttr_h": 0.0, "tts_h": None, "exposed": False, "disruptions": 0})
            t["disruptions"] += 1
            if d["ttr_h"] is not None:
                t["ttr_h"] = max(t["ttr_h"], d["ttr_h"])
            if d["tts_h"] is not None:
                t["tts_h"] = d["tts_h"] if t["tts_h"] is None else min(t["tts_h"], d["tts_h"])
            t["exposed"] = t["exposed"] or d["exposed"]
    return table


def compute_kpis(twin: "Twin", days: float) -> dict:
    k, net = twin.k, twin.net
    end = twin.env.now
    days = max(days, 1e-9)
    due = [o for o in twin.orders if o.promised_h <= end and o.created_h >= twin.stats_since_h]
    otif = [o for o in due if o.immediate == o.qty and o.done_h is not None and o.done_h <= o.promised_h]
    demanded = k["units_demanded"] or 1
    hours = k["hours"] or 1
    daily_demand = demanded / days
    per_sku = {}
    for sku, v in twin.k_sku.items():
        sku_due = [o for o in due if o.sku == sku]
        sku_otif = [o for o in otif if o.sku == sku]
        dd = v["units_demanded"] / days or 1
        per_sku[sku] = {
            "units_demanded": int(v["units_demanded"]),
            "fill_rate": v["units_filled_immediately"] / (v["units_demanded"] or 1),
            "otif": len(sku_otif) / (len(sku_due) or 1),
            "backorder_units_end": int(sum(twin.warehouses[dc].backlog_units(s) for dc, s in twin.pairs if s == sku)),
            "units_lost": int(v["units_lost"]),
            "avg_on_hand": v["on_hand_h"] / hours,
            "days_of_cover": v["on_hand_h"] / hours / dd,
        }
    per_dc = {f"{dc}/{sku}": {"on_hand_end": round(twin.warehouses[dc].level(sku)),
                              "backlog_end": int(twin.warehouses[dc].backlog_units(sku)),
                              "stockout_hours": twin.stockout_h[(dc, sku)]} for dc, sku in twin.pairs}
    cost = {"transport": k["transport_cost"], "holding": k["holding_cost"], "penalty": k["penalty_cost"],
            "ordering": k["ordering_cost"]}
    cost["total"] = sum(cost.values())
    on_hand_avg = sum(v["on_hand_h"] for v in twin.k_sku.values()) / hours
    disruptions = disruption_metrics(twin)
    return {
        "days": days, "start": twin.start.isoformat(timespec="minutes"), "seed": twin.seed,
        "orders": len([o for o in twin.orders if o.created_h >= twin.stats_since_h]), "orders_due": len(due),
        "fill_rate": k["units_filled_immediately"] / demanded,
        "otif": len(otif) / (len(due) or 1),
        "units_demanded": int(k["units_demanded"]),
        "units_backordered": int(k["units_backordered"]),
        "units_lost": int(k["units_lost"]),
        "backorder_units_end": int(sum(twin.warehouses[dc].backlog_units(s) for dc, s in twin.pairs)),
        "avg_on_hand": float(on_hand_avg), "inventory_days": float(on_hand_avg / daily_demand),
        "in_transit_value_inr": sum(s.qty * net.skus[s.sku].unit_value for s in twin.shipments.values()),
        "cost_inr": cost, "co2_t": k["co2_kg"] / 1000.0,
        "per_sku": per_sku, "per_dc_sku": per_dc,
        "stockout_episodes": len([e for e in twin.stockouts if e["start_h"] >= twin.stats_since_h]),
        "disruptions": disruptions,
        "tts_ttr": node_tts_ttr(disruptions),
        "replenishment_orders": int(k["replenishment_orders"]),
        "calibrated_lanes": twin.calibrated_lanes,
    }
