"""Demand model: base rate x weekly seasonality x festival spikes, negative-binomial noise.

    mean(zone, sku, day) = pop_m(zone) * base_demand_per_million_day(sku)
                           * dow[family][weekday] * festival(family, date) * scenario multiplier
    D ~ NegBin(mean, k)  via gamma-Poisson: lambda ~ Gamma(k, mean / k), D ~ Poisson(lambda)
    Var[D] = mean + mean^2 / k

Weekly profile and over-dispersion k per family are fitted from DataCo by
data/demand/fit_dataco.py (see data/demand/demand_params.json for windows, proxies and caveats).
"""
from __future__ import annotations

import json
from dataclasses import dataclass
from datetime import date, datetime, timedelta
from pathlib import Path

import numpy as np

from sim.macro.network import Network
from sim.paths import DATA


@dataclass(frozen=True)
class Festival:
    name: str
    day: date
    uplift: float
    families: tuple[str, ...]
    pre_days: int
    plateau_days: int
    post_days: int

    def shape(self, d: date) -> float:
        """0..1 intensity on day d (linear ramp up, plateau, linear decay)."""
        x = (d - self.day).days
        if x < -self.pre_days or x > self.post_days:
            return 0.0
        if x < -self.plateau_days:
            return (x + self.pre_days) / max(1, self.pre_days - self.plateau_days)
        if x <= 0:
            return 1.0
        return 1.0 - x / (self.post_days + 1)


class DemandModel:
    def __init__(self, net: Network, params_path: Path = DATA / "demand" / "demand_params.json",
                 festivals_path: Path = DATA / "demand" / "festivals.json"):
        self.net = net
        params = json.loads(params_path.read_text())
        self.dow = {f: np.array(v["dow"]) for f, v in params["families"].items()}
        self.k = {f: (v["nb_k"] if v["nb_k"] is not None else float("inf")) for f, v in params["families"].items()}
        self.festivals = [Festival(f["name"], date.fromisoformat(f["date"]), f["uplift"], tuple(f["families"]),
                                   f["pre_days"], f["plateau_days"], f["post_days"])
                          for f in json.loads(festivals_path.read_text())["festivals"]]

    def festival_multiplier(self, family: str, d: date) -> float:
        m = 1.0
        for f in self.festivals:
            if family in f.families:
                m += f.uplift * f.shape(d)
        return m

    def base_mean(self, zone: str, sku: str) -> float:
        """Units/day before seasonality."""
        return self.net.nodes[zone].attrs["pop_m"] * self.net.skus[sku].base_demand_per_million_day

    def mean(self, zone: str, sku: str, d: date) -> float:
        fam = self.net.skus[sku].family
        return self.base_mean(zone, sku) * self.dow[fam][d.weekday()] * self.festival_multiplier(fam, d)

    def variance(self, mean: float, sku: str) -> float:
        k = self.k[self.net.skus[sku].family]
        return mean + mean * mean / k

    def sample(self, rng: np.random.Generator, zone: str, sku: str, d: date, multiplier: float = 1.0) -> int:
        mu = self.mean(zone, sku, d) * multiplier
        if mu <= 0:
            return 0
        k = self.k[self.net.skus[sku].family]
        lam = mu if k == float("inf") else rng.gamma(k, mu / k)
        return int(rng.poisson(lam))

    def profile(self, zone: str, sku: str, start: datetime, days: int) -> list[float]:
        """Expected daily demand over a horizon (for planning and plots)."""
        return [self.mean(zone, sku, (start + timedelta(days=i)).date()) for i in range(days)]
