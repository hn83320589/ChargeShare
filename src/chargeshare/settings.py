import os
from dataclasses import dataclass, field
from pathlib import Path

from chargeshare.domain.allocation import PowerConfig

PROJECT_ROOT = Path(__file__).resolve().parents[2]


@dataclass(frozen=True)
class Settings:
    database_url: str = f"sqlite:///{PROJECT_ROOT / 'data' / 'chargeshare.db'}"
    config_dir: Path = PROJECT_ROOT / "config"
    heartbeat_interval_s: int = 60
    snapshot_interval_s: int = 30
    power: PowerConfig = field(default_factory=PowerConfig)
    admin_username: str = "admin"
    admin_password: str = os.environ.get("CHARGESHARE_ADMIN_PASSWORD", "admin")
    secret_key: str = os.environ.get("CHARGESHARE_SECRET_KEY", "dev-only-change-me")
