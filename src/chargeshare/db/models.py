"""資料表定義。功率一律為 W、電量為 Wh（整數）。"""

from datetime import datetime
from decimal import Decimal

from sqlalchemy import ForeignKey, Index, String, UniqueConstraint
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column, relationship

from chargeshare.db.types import DecimalText, TZDateTime


class Base(DeclarativeBase):
    type_annotation_map = {datetime: TZDateTime, Decimal: DecimalText}


class Household(Base):
    __tablename__ = "households"

    id: Mapped[int] = mapped_column(primary_key=True)
    unit_no: Mapped[str] = mapped_column(String(16), unique=True)  # 戶號
    password_hash: Mapped[str] = mapped_column(String(256))

    cards: Mapped[list["Card"]] = relationship(back_populates="household")
    bills: Mapped[list["Bill"]] = relationship(back_populates="household")


class Card(Base):
    __tablename__ = "cards"

    id_tag: Mapped[str] = mapped_column(String(32), primary_key=True)
    household_id: Mapped[int] = mapped_column(ForeignKey("households.id"), index=True)
    active: Mapped[bool] = mapped_column(default=True)

    household: Mapped[Household] = relationship(back_populates="cards")


class Charger(Base):
    __tablename__ = "chargers"

    id: Mapped[str] = mapped_column(String(32), primary_key=True)
    ocpp_status: Mapped[str] = mapped_column(String(32), default="Unavailable")
    connected: Mapped[bool] = mapped_column(default=False)
    last_seen_at: Mapped[datetime | None]
    disconnected_at: Mapped[datetime | None]
    # 最後一次樁確認（ACK）的上限；已送出但未確認的上限另存，以便斷線時保守保留
    assigned_limit_w: Mapped[int] = mapped_column(default=0)
    pending_limit_w: Mapped[int | None]


class Transaction(Base):
    __tablename__ = "transactions"
    # 語意層去重：同一支樁同一時間點只會開始一筆交易
    __table_args__ = (UniqueConstraint("charger_id", "start_ts"),)

    id: Mapped[int] = mapped_column(primary_key=True)
    charger_id: Mapped[str] = mapped_column(ForeignKey("chargers.id"), index=True)
    household_id: Mapped[int] = mapped_column(ForeignKey("households.id"), index=True)
    id_tag: Mapped[str] = mapped_column(String(32))
    start_ts: Mapped[datetime]
    meter_start_wh: Mapped[int]
    stop_ts: Mapped[datetime | None]
    meter_stop_wh: Mapped[int | None]
    stop_reason: Mapped[str | None] = mapped_column(String(32))
    # 結束後計算
    peak_wh: Mapped[int | None]
    offpeak_wh: Mapped[int | None]
    amount: Mapped[Decimal | None]  # 精確金額，帳單層才四捨五入
    bill_id: Mapped[int | None] = mapped_column(ForeignKey("bills.id"), index=True)

    charger: Mapped[Charger] = relationship()
    household: Mapped[Household] = relationship()
    samples: Mapped[list["MeterSample"]] = relationship(
        back_populates="transaction", order_by="MeterSample.ts"
    )
    bill: Mapped["Bill | None"] = relationship(back_populates="transactions")

    @property
    def is_active(self) -> bool:
        return self.stop_ts is None


class MeterSample(Base):
    __tablename__ = "meter_samples"
    # 語意層去重：補傳或重送的同一筆讀數只存一次
    __table_args__ = (UniqueConstraint("transaction_id", "ts"),)

    id: Mapped[int] = mapped_column(primary_key=True)
    transaction_id: Mapped[int] = mapped_column(ForeignKey("transactions.id"))
    ts: Mapped[datetime]
    energy_wh: Mapped[int]
    power_w: Mapped[int]

    transaction: Mapped[Transaction] = relationship(back_populates="samples")


class ProcessedMessage(Base):
    """訊息層去重：記錄已處理的 (樁, messageId) 及當時的回應，重送時原樣回覆。"""

    __tablename__ = "processed_messages"

    charger_id: Mapped[str] = mapped_column(String(32), primary_key=True)
    message_id: Mapped[str] = mapped_column(String(64), primary_key=True)
    action: Mapped[str] = mapped_column(String(32))
    response_json: Mapped[str]
    received_at: Mapped[datetime]


class Bill(Base):
    __tablename__ = "bills"
    __table_args__ = (UniqueConstraint("household_id", "period"),)

    id: Mapped[int] = mapped_column(primary_key=True)
    household_id: Mapped[int] = mapped_column(ForeignKey("households.id"))
    period: Mapped[str] = mapped_column(String(7))  # "2026-09"
    issued_at: Mapped[datetime]
    peak_wh: Mapped[int]
    offpeak_wh: Mapped[int]
    total_amount: Mapped[int]  # 元，已四捨五入
    paid_at: Mapped[datetime | None]

    household: Mapped[Household] = relationship(back_populates="bills")
    transactions: Mapped[list[Transaction]] = relationship(
        back_populates="bill", order_by="Transaction.start_ts"
    )


class PowerSnapshot(Base):
    """儀表板總功率曲線的取樣點。"""

    __tablename__ = "power_snapshots"
    __table_args__ = (Index("ix_power_snapshots_ts", "ts"),)

    id: Mapped[int] = mapped_column(primary_key=True)
    ts: Mapped[datetime]
    measured_w: Mapped[int]
    limit_w: Mapped[int]
    reserved_w: Mapped[int]


class BillingRun(Base):
    """每期只結算一次；之後才結束的交易併入下一期。"""

    __tablename__ = "billing_runs"

    period: Mapped[str] = mapped_column(String(7), primary_key=True)
    ran_at: Mapped[datetime]
