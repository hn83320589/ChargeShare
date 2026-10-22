import os
from dataclasses import dataclass, field
from pathlib import Path

from chargeshare.domain.allocation import PowerConfig

PROJECT_ROOT = Path(__file__).resolve().parents[2]

# 僅供本機展示；正式部署請以環境變數覆寫
DEFAULT_ADMIN_PASSWORD = "admin"
DEFAULT_SECRET_KEY = "dev-only-change-me"


@dataclass(frozen=True)
class Settings:
    database_url: str = f"sqlite:///{PROJECT_ROOT / 'data' / 'chargeshare.db'}"
    config_dir: Path = PROJECT_ROOT / "config"
    heartbeat_interval_s: int = 60
    snapshot_interval_s: int = 30
    power: PowerConfig = field(default_factory=PowerConfig)
    admin_username: str = "admin"
    admin_password: str = os.environ.get("CHARGESHARE_ADMIN_PASSWORD", DEFAULT_ADMIN_PASSWORD)
    secret_key: str = os.environ.get("CHARGESHARE_SECRET_KEY", DEFAULT_SECRET_KEY)

    def insecure_defaults(self) -> list[str]:
        """仍使用開發用預設值的設定（正式部署前必須以環境變數覆寫）。"""
        found = []
        if self.admin_password == DEFAULT_ADMIN_PASSWORD:
            found.append("CHARGESHARE_ADMIN_PASSWORD")
        if self.secret_key == DEFAULT_SECRET_KEY:
            found.append("CHARGESHARE_SECRET_KEY")
        return found
