# 第 5 章　資料模型與持久化：讓資料庫幫你守規則

## 學習目標

- 設計能表達「進行中」與「不確定」狀態的資料表
- 處理 SQLite 的兩個陷阱：**時區**與**精確小數**
- 用**唯一約束**實作冪等，而不是靠程式記得檢查
- 用 Alembic 管理資料表演進

---

## 5.1 資料表一覽

| 資料表 | 用途 | 關鍵約束 |
|---|---|---|
| `households` | 戶 | `unit_no` 唯一 |
| `cards` | RFID 卡，可停用 | 主鍵 `id_tag` |
| `chargers` | 樁的上限與連線狀態 | `assigned_limit_w`、`pending_limit_w` |
| `transactions` | 一次充電 | `UNIQUE(charger_id, start_ts)` |
| `meter_samples` | 每分鐘讀數 | `UNIQUE(transaction_id, ts)` |
| `processed_messages` | 已處理的訊息與回應 | 主鍵 `(charger_id, message_id)` |
| `bills` | 月結帳單 | `UNIQUE(household_id, period)` |
| `billing_runs` | 每期只結算一次 | 主鍵 `period` |
| `power_snapshots` | 儀表板曲線 | `ts` 索引 |

模型定義在 [`db/models.py`](../../src/chargeshare/db/models.py)，使用 SQLAlchemy 2.x 的型別化寫法：

```python
class MeterSample(Base):
    __tablename__ = "meter_samples"
    # 語意層去重：補傳或重送的同一筆讀數只存一次
    __table_args__ = (UniqueConstraint("transaction_id", "ts"),)

    id: Mapped[int] = mapped_column(primary_key=True)
    transaction_id: Mapped[int] = mapped_column(ForeignKey("transactions.id"))
    ts: Mapped[datetime]
    energy_wh: Mapped[int]
    power_w: Mapped[int]
```

---

## 5.2 陷阱一：SQLite 會默默丟掉時區

SQLAlchemy 的 `DateTime(timezone=True)` 在 PostgreSQL 上會保存時區，但在 **SQLite 上不會**。
本系統的時間可能來自：樁送的 `+08:00`、樁送的 `Z`（UTC）、伺服器的台灣時間。
混著存而不轉換，比較大小就會錯 8 小時。

解法是自訂型別：一律轉成 UTC 寫入，讀出時轉回台灣時間；**拒絕沒有時區的 datetime**。

```python
# src/chargeshare/db/types.py
class TZDateTime(TypeDecorator[datetime]):
    impl = DateTime
    cache_ok = True

    def process_bind_param(self, value, dialect):
        if value is None:
            return None
        if value.tzinfo is None:
            raise ValueError(f"datetime must be timezone-aware, got naive {value!r}")
        return value.astimezone(UTC).replace(tzinfo=None)

    def process_result_value(self, value, dialect):
        if value is None:
            return None
        return value.replace(tzinfo=UTC).astimezone(TAIPEI)
```

拒絕 naive datetime 是 **fail fast** 的應用：寧可在寫入時就報錯，也不要存進一筆含糊的時間、三個月後算錯帳。

並透過 `type_annotation_map` 讓所有 `Mapped[datetime]` 自動使用這個型別，不會漏掉任何一欄：

```python
class Base(DeclarativeBase):
    type_annotation_map = {datetime: TZDateTime, Decimal: DecimalText}
```

## 5.3 陷阱二：SQLite 沒有精確小數

SQLite 的 `NUMERIC` 實際上可能以浮點數儲存。金額 `154.7032` 存進去再讀出來可能多出尾數。
解法同上：`DecimalText` 以字串儲存 `Decimal`。

> 💡 **教學重點**：ORM 讓你以為換資料庫只要改連線字串。實際上每種資料庫在時間、小數、
> 交易隔離上都有差異。用 TypeDecorator 把差異收在一個地方，並用測試釘住行為
> （`tests/integration/test_db.py`）。

---

## 5.4 用約束實作冪等

「同一筆讀數重送 3 次只算一次」可以在程式裡檢查，也可以交給資料庫：

```python
__table_args__ = (UniqueConstraint("transaction_id", "ts"),)
```

兩者都做是最穩的：程式先查（為了回傳友善的結果、不靠例外控制流程），
資料庫約束是最後防線（即使程式有 bug 或兩個請求同時進來，也不會存兩筆）。

本系統的四個冪等約束各自防止一種重複：

| 約束 | 防止什麼 |
|---|---|
| `processed_messages(charger_id, message_id)` | 同一則訊息原封不動重送 |
| `meter_samples(transaction_id, ts)` | 樁重開機後用**新的** messageId 重送同一筆讀數 |
| `transactions(charger_id, start_ts)` | `StartTransaction` 重送時建立第二筆交易 |
| `bills(household_id, period)` | 同一期重複開帳單 |

---

## 5.5 表達「進行中」與「不確定」

### 交易的生命週期

交易沒有 `status` 欄位，而是由 `stop_ts` 是否為空推導：

```python
@property
def is_active(self) -> bool:
    return self.stop_ts is None
```

### 樁的上限：兩個欄位

```python
# 最後一次樁確認（ACK）的上限；已送出但未確認的上限另存，以便斷線時保守保留
assigned_limit_w: Mapped[int] = mapped_column(default=0)
pending_limit_w: Mapped[int | None]
```

送出指令前先寫入 `pending`，收到確認才更新 `assigned`。如果伺服器在中間重啟，
啟動時從資料庫讀到 `pending` 仍有值，就知道「那支樁可能用的是這個值」，保守地以兩者較大者計算。

### 電表重置的審查狀態

```python
meter_resets: Mapped[int] = mapped_column(default=0, server_default="0")
reset_reviewed_at: Mapped[datetime | None]

@property
def needs_review(self) -> bool:
    return self.meter_resets > 0 and self.reset_reviewed_at is None
```

用「發生次數 + 確認時間」兩欄，而不是一個布林值，可以保留稽核資訊：何時被確認、發生過幾次。

---

## 5.6 Alembic：資料表的版本控制

資料表結構會隨需求演進。本專案有三個 migration：

| 版本 | 內容 | 起因 |
|---|---|---|
| 0001 | 初始結構 | — |
| 0002 | `billing_runs` | 月結需要「每期只結算一次」 |
| 0003 | `meter_resets`、`reset_reviewed_at` | 審查發現電表重置問題 |

兩個實務重點：

1. **SQLite 不支援大部分 ALTER TABLE**，所以 `env.py` 設定 `render_as_batch=True`，
   讓 Alembic 用「建新表、複製資料、換名」的方式修改。
2. **新增 NOT NULL 欄位要給 `server_default`**，否則既有資料列會違反約束。

另外有一個測試確保 migration 與 model 永遠一致：

```python
# tests/integration/test_db.py
def test_migrations_match_models(tmp_path):
    engine = create_db_engine(f"sqlite:///{tmp_path / 'migrated.db'}")
    upgrade_to_head(engine)
    with engine.connect() as conn:
        diff = compare_metadata(MigrationContext.configure(conn), Base.metadata)
    assert diff == []
```

改了 model 卻忘了產生 migration，這個測試就會失敗。

---

## 5.7 批次寫入的陷阱

範例資料要寫入十幾萬筆讀數，逐筆用 ORM 物件太慢，所以用 Core 的批次 insert：

```python
rows = [{"transaction_id": tx.id, "ts": ts, ...} for ts, energy, power_w in s.samples[:-1]]
if rows:  # 空串列會讓 SQLAlchemy 產生 INSERT ... DEFAULT VALUES
    session.execute(insert(MeterSample), rows)
```

那個 `if rows:` 是後來補上的（第 10 章案例 10）：空串列不是「什麼都不做」，
而是產生一句插入預設值的 SQL，違反 NOT NULL 約束。

---

## 小結

1. 用 TypeDecorator 收斂資料庫差異，並拒絕含糊的資料。
2. 冪等交給唯一約束當最後防線。
3. 用欄位組合表達「進行中」「不確定」「待確認」。
4. migration 與 model 的一致性要有測試保證。
5. 批次操作要測邊界情況（空集合）。

## 思考題

1. `processed_messages` 會無限增長。設計一個清理策略：保留多久？清理時要注意什麼？
2. 如果要支援 PostgreSQL，`TZDateTime` 與 `DecimalText` 還需要嗎？
3. `bills.total_amount` 存的是四捨五入後的整數。如果費率設定改了，舊帳單要重算嗎？資料模型怎麼支援這個決定？
