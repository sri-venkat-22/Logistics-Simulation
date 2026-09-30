"""Repository paths shared by the simulation packages."""
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
DATA = ROOT / "data"
SCENARIO_TEMPLATES = ROOT / "sim" / "scenarios" / "templates"
HYDERABAD = ROOT / "sim" / "micro" / "hyderabad"
