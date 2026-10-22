"""產生 README 的「先降後升」對照圖：docs/images/two-phase.svg

以真正的 PowerManager 跑一次「8 支各 3.75 kW，其中 2 支快充滿」的情境，記錄它實際下達的
SetChargingProfile，再以「下達後 10 秒內新舊值都可能生效，取較大者」計算每秒的最壞情況總功率；
對照組為同一組目標上限「同時下達」時的最壞情況。

    uv run python scripts/two_phase_chart.py
"""

import asyncio
import sys
import tempfile
from datetime import datetime, timedelta
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from chargeshare.clock import TAIPEI, FakeClock  # noqa: E402
from chargeshare.db.models import Base, Charger  # noqa: E402
from chargeshare.db.session import create_db_engine, make_session_factory  # noqa: E402
from chargeshare.domain.allocation import PowerConfig  # noqa: E402
from chargeshare.services.events import (  # noqa: E402
    ChargerConnected,
    MeterSampled,
    SessionStarted,
)
from chargeshare.services.power_manager import PowerManager  # noqa: E402
from tests.integration.power_helpers import SETTLE, FakeLink  # noqa: E402

T0 = datetime(2026, 10, 22, 18, 0, tzinfo=TAIPEI)
IDS = [f"CP0{i}" for i in range(1, 9)]
TAPERING = ("CP01", "CP02")
OUTPUT = ROOT / "docs" / "images" / "two-phase.svg"


async def record_sends() -> tuple[list[tuple[datetime, str, int]], datetime]:
    with tempfile.TemporaryDirectory() as tmp:
        engine = create_db_engine(f"sqlite:///{tmp}/chart.db")
        Base.metadata.create_all(engine)
        factory = make_session_factory(engine)
        with factory.begin() as session:
            session.add_all(Charger(id=cid) for cid in IDS)
        clock = FakeClock(T0)
        link = FakeLink(clock)
        manager = PowerManager(factory, clock, link, PowerConfig())
        manager.load()
        for index, cid in enumerate(IDS, start=1):
            link.online.add(cid)
            manager.publish(ChargerConnected(cid))
            manager.publish(SessionStarted(cid, index, T0))
        await clock.advance(30)
        for _ in range(2):  # 連續 2 筆低功率讀數 → 判定快充滿
            await clock.advance(60)
            for cid in IDS:
                manager.publish(
                    MeterSampled(cid, 0, clock.now(), 800 if cid in TAPERING else 3_700)
                )
        mark = clock.now()
        for _ in range(6):
            await clock.advance(10)
        engine.dispose()
        return link.sends, mark


def worst_case_at(sends: list[tuple[datetime, str, int]], at: datetime) -> int:
    total = 0
    for cid in IDS:
        value = 0
        for ts, charger_id, limit in sorted(sends):
            if charger_id != cid or ts > at:
                continue
            value = limit if at >= ts + SETTLE else max(value, limit)
        total += value
    return total


def series(sends, start: datetime, seconds: int) -> list[float]:
    return [worst_case_at(sends, start + timedelta(seconds=s)) / 1000 for s in range(seconds + 1)]


def simultaneous(sends, mark: datetime) -> list[tuple[datetime, str, int]]:
    """把同一次重算的調升提前到調降的同一時間送出。"""
    before = [s for s in sends if s[0] < mark]
    after = [(mark, cid, limit) for _, cid, limit in sends if _ >= mark]
    return before + after


def svg(two_phase: list[float], naive: list[float], lead: int, increase_at: int) -> str:
    """lead：圖上第 lead 秒為送出調降的時間點，刻度以此為 0。"""
    width, height, left, right, top, bottom = 760, 380, 60, 24, 76, 52
    plot_w, plot_h = width - left - right, height - top - bottom
    x_max, y_min, y_max = len(two_phase) - 1, 24.0, 36.0

    def x(sec: float) -> float:
        return left + plot_w * sec / x_max

    def y(kw: float) -> float:
        return top + plot_h * (y_max - kw) / (y_max - y_min)

    def path(values: list[float]) -> str:
        # 階梯線：功率上限在下達瞬間跳變
        points = [f"M{x(0):.1f},{y(values[0]):.1f}"]
        for sec in range(1, len(values)):
            points.append(f"H{x(sec):.1f}V{y(values[sec]):.1f}")
        return "".join(points)

    grid = []
    for kw in range(24, 37, 2):
        grid.append(
            f'<line x1="{left}" x2="{width - right}" y1="{y(kw):.1f}" y2="{y(kw):.1f}" '
            f'stroke="#e5e7eb"/><text x="{left - 8}" y="{y(kw) + 4:.1f}" text-anchor="end">{kw}</text>'
        )
    for sec in range(lead % 5, x_max + 1, 5):
        grid.append(
            f'<text x="{x(sec):.1f}" y="{height - bottom + 18}" text-anchor="middle">{sec - lead}</text>'
        )
    marks = [
        (lead, "送出調降 → ACK"),
        (increase_at, "等 10 秒後送出調升"),
    ]
    for sec, label in marks:
        grid.append(
            f'<line x1="{x(sec):.1f}" x2="{x(sec):.1f}" y1="{top}" y2="{height - bottom}" '
            f'stroke="#9ca3af" stroke-dasharray="3 3"/>'
            f'<text x="{x(sec) + 4:.1f}" y="{top + 14}" fill="#4b5563">{label}</text>'
        )
    return f"""<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 {width} {height}" font-family="system-ui, -apple-system, 'PingFang TC', 'Noto Sans TC', sans-serif" font-size="12">
  <rect width="100%" height="100%" fill="#ffffff"/>
  <text x="{left}" y="24" font-size="15" font-weight="600" fill="#111827">2 支樁快充滿時重新分配：最壞情況總功率（樁收到新上限後 10 秒內才生效）</text>
  {"".join(grid)}
  <path d="{path(naive)}" fill="none" stroke="#f59e0b" stroke-width="2.5"/>
  <path d="{path(two_phase)}" fill="none" stroke="#2563eb" stroke-width="4"/>
  <line x1="{left}" x2="{width - right}" y1="{y(30):.1f}" y2="{y(30):.1f}" stroke="#dc2626" stroke-width="1.5" stroke-dasharray="6 4"/>
  <text x="{width - right - 4}" y="{y(30) + 16:.1f}" text-anchor="end" fill="#dc2626">契約上限 30 kW</text>
  <text x="{left + plot_w / 2:.1f}" y="{height - 10}" text-anchor="middle" fill="#4b5563">秒（以送出調降為 0）</text>
  <text x="14" y="{top + plot_h / 2:.1f}" transform="rotate(-90 14 {top + plot_h / 2:.1f})" text-anchor="middle" fill="#4b5563">kW</text>
  <g transform="translate({left},52)">
    <line x1="0" x2="22" y1="-4" y2="-4" stroke="#2563eb" stroke-width="3"/><text x="28" y="0">先降後升（實際）：最高 {max(two_phase):.1f} kW</text>
    <line x1="210" x2="232" y1="-4" y2="-4" stroke="#f59e0b" stroke-width="2.5"/><text x="238" y="0">同時下達：最高 {max(naive):.1f} kW</text>
  </g>
</svg>
"""


def main() -> None:
    sends, mark = asyncio.run(record_sends())
    decrease = min(s[0] for s in sends if s[0] >= mark)
    increase = max(s[0] for s in sends if s[0] >= mark)
    lead = 5
    start = decrease - timedelta(seconds=lead)
    seconds = 35
    two_phase = series(sends, start, seconds)
    naive = series(simultaneous(sends, mark), start, seconds)
    OUTPUT.parent.mkdir(parents=True, exist_ok=True)
    OUTPUT.write_text(
        svg(two_phase, naive, lead, int((increase - start).total_seconds())),
        encoding="utf-8",
    )
    print(f"先降後升最壞情況 {max(two_phase):.2f} kW、同時下達 {max(naive):.2f} kW → {OUTPUT}")


if __name__ == "__main__":
    main()
