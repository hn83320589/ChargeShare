from datetime import datetime
from pathlib import Path

import pytest

from chargeshare.clock import TAIPEI
from chargeshare.config import load_tariff
from chargeshare.domain.tariff import Tariff

CONFIG_DIR = Path(__file__).resolve().parents[2] / "config"


@pytest.fixture(scope="session")
def tariff() -> Tariff:
    return load_tariff(CONFIG_DIR / "tariff.toml", CONFIG_DIR / "holidays_2026.toml")


def tw(*args: int) -> datetime:
    return datetime(*args, tzinfo=TAIPEI)
