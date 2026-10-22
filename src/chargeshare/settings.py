import os
from dataclasses import dataclass, field
from pathlib import Path

from chargeshare.domain.allocation import PowerConfig

PROJECT_ROOT = Path(__file__).resolve().parents[2]

# 僅供本機展示；必須明確以 --dev 啟動才允許使用
DEFAULT_ADMIN_PASSWORD = "admin"


class InsecureConfiguration(RuntimeError):
    pass


def _env(name: str, default: str | None = None):
    # 建立 Settings 時才讀取環境變數，而不是 import 時
    return field(default_factory=lambda: os.environ.get(name, default))


@dataclass(frozen=True)
class Settings:
    database_url: str = f"sqlite:///{PROJECT_ROOT / 'data' / 'chargeshare.db'}"
    config_dir: Path = PROJECT_ROOT / "config"
    heartbeat_interval_s: int = 60
    snapshot_interval_s: int = 30
    power: PowerConfig = field(default_factory=PowerConfig)
    admin_username: str = "admin"
    admin_password: str = _env("CHARGESHARE_ADMIN_PASSWORD", DEFAULT_ADMIN_PASSWORD)
    # 未設定時於首次啟動自動產生隨機金鑰，存在資料庫旁的 secret_key 檔
    secret_key: str | None = _env("CHARGESHARE_SECRET_KEY")
    dev_mode: bool = False

    @property
    def secret_key_path(self) -> Path:
        if self.database_url.startswith("sqlite:///"):
            return Path(self.database_url.removeprefix("sqlite:///")).parent / "secret_key"
        return PROJECT_ROOT / "data" / "secret_key"

    def ensure_secure(self) -> None:
        """管理員密碼仍是公開的預設值時拒絕啟動，除非明確以開發模式執行。"""
        if self.admin_password == DEFAULT_ADMIN_PASSWORD and not self.dev_mode:
            raise InsecureConfiguration(
                "管理員密碼仍是預設值。請設定環境變數 CHARGESHARE_ADMIN_PASSWORD，"
                "或在本機展示時加上 --dev。"
            )
