from datetime import datetime
from zoneinfo import ZoneInfo

import pytest

from sim.macro.demand import DemandModel
from sim.macro.network import Network

START = datetime(2026, 10, 15, tzinfo=ZoneInfo("Asia/Kolkata"))


@pytest.fixture(scope="session")
def net() -> Network:
    return Network()


@pytest.fixture(scope="session")
def demand(net: Network) -> DemandModel:
    return DemandModel(net)
