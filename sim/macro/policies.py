"""Inventory replenishment policies for a Warehouse x SKU.

    SSPolicy(s, S)            (s, S): when inventory position <= s, order up to S
    RQPolicy(r, q)            (R, nQ): when inventory position <= r, order the smallest multiple of q that lifts it above r
    DynamicSSPolicy(z, cover) (s, S) recomputed at every review from the forward-looking demand forecast and the
                              lead-time distribution of the primary path (the national twin's default)

Inventory position = on hand + on order - backlog. `review_h` is the review period in hours
(24 = daily periodic review at REVIEW_HOUR, 0 = continuous review after every withdrawal).
"""
from __future__ import annotations

import math
from dataclasses import dataclass
from datetime import timedelta
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from sim.macro.engine import Twin

# Default policy per SKU family: z = safety factor, cover_days = forecast days added to the order-up-to level.
# Cold-chain vaccine storage is scarce and expensive (lean); sea-imported electronics are ordered in batches.
FAMILY_POLICY = {"vaccine": {"z": 2.05, "cover_days": 2}, "fmcg": {"z": 1.65, "cover_days": 5},
                 "electronics": {"z": 1.65, "cover_days": 10}}


class Policy:
    name = "policy"
    review_h: float = 24.0
    lost_sales: bool = False  # True: unmet demand is lost instead of backordered

    def levels(self, twin: Twin, dc: str, sku: str, t_h: float) -> tuple[float, float]:
        """(reorder point, order-up-to or reorder point + Q) at time t — for display and seeding."""
        raise NotImplementedError

    def order_qty(self, twin: Twin, dc: str, sku: str, ip: float, t_h: float) -> float:
        raise NotImplementedError

    def describe(self) -> dict:
        return {"name": self.name, "review_h": self.review_h, "lost_sales": self.lost_sales}


@dataclass
class SSPolicy(Policy):
    s: float
    S: float
    review_h: float = 24.0
    lost_sales: bool = False
    name: str = "(s,S)"

    def levels(self, twin, dc, sku, t_h):
        return self.s, self.S

    def order_qty(self, twin, dc, sku, ip, t_h):
        return self.S - ip if ip <= self.s else 0.0

    def describe(self):
        return {**super().describe(), "s": self.s, "S": self.S}


@dataclass
class RQPolicy(Policy):
    r: float
    q: float
    review_h: float = 0.0
    lost_sales: bool = False
    name: str = "(R,Q)"

    def levels(self, twin, dc, sku, t_h):
        return self.r, self.r + self.q

    def order_qty(self, twin, dc, sku, ip, t_h):
        if ip > self.r:
            return 0.0
        return self.q * max(1, math.floor((self.r - ip) / self.q) + 1)

    def describe(self):
        return {**super().describe(), "R": self.r, "Q": self.q}


@dataclass
class DynamicSSPolicy(Policy):
    """s covers forecast demand over lead time + review period at service level z (demand and lead-time
    variance); S adds `cover_days` of forecast demand after the lead time, so known festivals are pre-built
    and unannounced shocks (scenarios) are not foreseen."""
    z: float
    cover_days: float
    review_h: float = 24.0
    lost_sales: bool = False
    name: str = "dynamic (s,S)"

    def levels(self, twin, dc, sku, t_h):
        L, varL = twin.lead_stats_days(dc, sku)
        R = max(self.review_h, 1.0) / 24.0
        horizon = L + R
        zones = twin.zones_served(dc, sku)
        day0 = (twin.start + timedelta(hours=t_h)).date()
        days = int(math.ceil(horizon))
        fc = getattr(twin, "forecaster", None) or twin.demand  # an ML forecaster (ml.forecast) can replace the model
        mu_h = var_h = 0.0
        for i in range(days):
            frac = min(1.0, horizon - i)
            for z in zones:
                m = fc.mean(z, sku, day0 + timedelta(days=i))
                mu_h += m * frac
                var_h += twin.demand.variance(m, sku) * frac
        mu_d = mu_h / horizon if horizon else 0.0
        s = mu_h + self.z * math.sqrt(var_h + mu_d * mu_d * varL)
        cover = sum(fc.mean(z, sku, day0 + timedelta(days=days + i))
                    for i in range(int(self.cover_days)) for z in zones)
        return s, s + cover

    def order_qty(self, twin, dc, sku, ip, t_h):
        s, S = self.levels(twin, dc, sku, t_h)
        return math.ceil(S - ip) if ip < s else 0.0

    def describe(self):
        return {**super().describe(), "z": self.z, "cover_days": self.cover_days}


def default_policy(family: str, overrides: dict | None = None) -> Policy:
    cfg = {**FAMILY_POLICY[family], **((overrides or {}).get(family, {}))}
    return DynamicSSPolicy(z=cfg["z"], cover_days=cfg["cover_days"])
