# ChargeShare 系統分析與架構設計（草案 v0.1）

> 狀態：已確認並實作完成（決策見第 9 節）。實作細節與使用方式見 README。

---

## 1. 需求分析重點

把題目拆成 4 個核心問題，其餘都是周邊：

| 核心問題 | 難點 | 對應驗收 |
|---|---|---|
| **功率分配** | 30 kW 硬上限、6A 下限、需求感知（快充滿釋放額度）、防振盪、排隊 | 3, 4, 5, 12 |
| **計量與計費** | 每分鐘讀數分攤到時段、跨日跨季、國定假日、四捨五入 | 6, 7, 11 |
| **不可靠的樁** | 斷線補傳、重送去重、重開機、斷線保留額度 | 8, 9, 10, 13 |
| **時間** | 全部邏輯可注入時鐘，模擬器要能加速 | 10, 13 |

### 1.1 題目中隱含、需要先講清楚的地方

1. **「任何時刻」不超過 30 kW** —— 樁在收到新上限後最多 10 秒才生效。如果同時對 A 樁降、B 樁升，B 先生效、A 還沒降的那幾秒，總和會超標。→ 需要「**先降後升**」的兩階段下達（見 4.4）。
2. **斷線樁仍在供電** —— 斷線 ≠ 停止充電。伺服器看不到它，但它可能還在依最後的上限抽電。→ 從斷線那一刻起就保留其額度，直到重新連線、確認狀態為止（比題目要求的 5 分鐘更保守）。
3. **補傳資料是「過去的」** —— 補傳的 `MeterValues` 時間戳是過去時間，計費必須以**樁的時間戳**為準；但功率分配不能拿過期的功率值做決策。
4. **驗收 7 的陷阱** —— 2026/5/31 是週日、6/1 是週一。整段 22:00–08:00 都是離峰，但午夜後費率從非夏月 1.9 跳到夏月 2.0。重點是「季節依每筆讀數的時間判斷」，而非依充電開始時間。
5. **重開機** —— 電表是累計值、硬體保存，重開機不歸零；但樁重開機後可能忘記功率上限，必須在 `BootNotification` 後立即重送。

---

## 2. 系統架構

```
                ┌──────────────────────────── FastAPI process (單一 asyncio event loop) ───────────────────────────┐
                │                                                                                                 │
 ┌──────────┐   │  ┌────────────────┐   ┌──────────────────────┐   ┌─────────────────────────────┐                │
 │ 充電樁 x8 │◀─WS─▶│ ocpp/ 連線層    │──▶│ services/             │──▶│ domain/ （純函數、無 IO）     │                │
 │ (或模擬器)│   │  │ - frame 解析    │   │ - TransactionService │   │ - allocation  功率分配       │                │
 └──────────┘   │  │ - 訊息去重       │   │ - PowerManager       │   │ - demand      需求/防振盪     │                │
                │  │ - ConnectionMgr │◀──│   (重算 + 兩階段下達) │   │ - tariff      時段判定        │                │
                │  └────────────────┘   │ - BillingService     │   │ - metering    電量分攤        │                │
                │                       │ - Scheduler (月結)    │   │ - billing     金額計算        │                │
 ┌──────────┐   │  ┌────────────────┐   └──────────┬───────────┘   │ - auth_rules  授權規則        │                │
 │ 瀏覽器     │◀─HTTP─▶│ web/ Jinja2+HTMX│              │               └─────────────────────────────┘                │
 │ 管委會/住戶 │   │  │ Chart.js 曲線   │──────────────┤                                                              │
 └──────────┘   │  └────────────────┘              ▼                                                              │
                │                       ┌──────────────────────┐        ┌─────────┐                               │
                │                       │ db/ SQLAlchemy 2.x   │───────▶│ SQLite  │  (Alembic migration)          │
                │                       └──────────────────────┘        └─────────┘                               │
                │                         ▲ 所有時間都來自注入的 Clock（System / Fake / Scaled）                       │
                └─────────────────────────────────────────────────────────────────────────────────────────────────┘
```

### 2.1 分層原則

- **domain/**：純函數、不碰 DB 與時鐘（時間以參數傳入）。驗收 3/4/5/6/7/11 全部在這層用單元測試達成。
- **services/**：協調 domain 與 DB/連線，持有可注入的 `Clock`。
- **ocpp/**、**web/**：薄薄的 I/O 轉接層。
- 整個 app 由 `create_app(settings, clock)` 組裝，測試時注入 `FakeClock` 與 in-memory / 暫存 SQLite。

### 2.2 目錄結構（預定）

```
ChargeShare/
├── pyproject.toml / uv.lock / alembic.ini / README.md
├── config/
│   ├── tariff.toml            # 費率、季節、尖離峰時段、管理費
│   ├── holidays_2026.toml     # 2026 國定假日
├── src/chargeshare/
│   ├── clock.py               # Clock protocol + System/Fake/Scaled
│   ├── domain/                # allocation, demand, tariff, metering, billing, auth_rules
│   ├── db/                    # models.py, session.py, migrations/
│   ├── ocpp/                  # protocol.py(frame/payload), router.py, connections.py
│   ├── services/              # transactions, power_manager, billing, scheduler
│   ├── web/                   # admin, resident, api, templates/, static/
│   ├── seed.py                # 範例資料產生
│   ├── app.py / cli.py
│   └── simulator/             # charger.py, ev.py, scenarios.py, recorder.py, cli.py
├── tests/{unit,integration,e2e}/
└── .github/workflows/ci.yml
```

---

## 3. 資料模型

內部單位一律用**整數**：功率 W、電量 Wh，避免浮點誤差；只有顯示時轉 kW / kWh。金額用 `Decimal`。

```
households      id, unit_no(戶號, unique), password_hash
cards           id_tag(PK), household_id, active, created_at
chargers        id(如 CP01), ocpp_status, connected, last_seen_at,
                assigned_limit_w(最後下達), pending_limit_w(已送未確認), firmware…
transactions    id, charger_id, id_tag, household_id,
                start_ts, meter_start_wh, stop_ts, meter_stop_wh, stop_reason,
                state(active/stopped), queued_at(排隊順序依據),
                peak_wh, offpeak_wh, amount(停止後計算，可重算), bill_id(nullable)
meter_samples   id, transaction_id, ts, energy_wh, power_w
                UNIQUE(transaction_id, ts)          ← 去重第二層
processed_msgs  charger_id, message_id, action, response_json, received_at
                UNIQUE(charger_id, message_id)      ← 去重第一層
bills           id, household_id, period('2026-09'), issued_at,
                peak_wh, offpeak_wh, total_amount(元, 整數), paid_at(nullable)
                UNIQUE(household_id, period)        ← 月結冪等
power_snapshots ts, measured_w, limit_w, reserved_w  ← 儀表板曲線
billing_runs    period(PK), ran_at                   ← 每期只結算一次
```

---

## 4. 功率分配演算法

### 4.1 輸入 / 輸出（純函數）

```python
def allocate(chargers: list[ChargerState], capacity_w=30_000,
             min_w=1_400, max_w=7_000, step_w=50) -> Allocation
# ChargerState: id, online, has_session, arrived_at, demand_cap_w, last_limit_w
# Allocation:   limits: dict[id, W], queued: list[id], reserved_w
```

### 4.2 步驟

1. **保留額度**：有進行中交易但**離線**的樁，保留其最後下達（或已送出未確認，取較大者）的上限。`budget = 30kW − reserved`。
2. **決定誰能供電**：線上且有交易的樁依 `arrived_at` 排序（先來後到）。最多 `n = floor(budget / 1.4kW)` 支供電，其餘上限 0、狀態「排隊中」。
   - 22 支 → `floor(30/1.4) = 21`，第 22 支排隊（驗收 5）。有人結束，下一次重算自然由排第一的補上。
   - 已在供電者不會被後到者搶走（不搶佔）。
3. **注水法（water-filling）分配**：每支有自己的上限 `cap_i`（一般為 7 kW；快充滿者為其需求值，見 4.3），找一個水位 `L` 使 `Σ min(cap_i, L) = budget`，每支 `clamp(min(cap_i, L), 1.4, 7)`。
4. **量化**：共同水位一次**向下**取整到 50 W（3.75 kW、1.4 kW 皆可整除），保證總和 ≤ 30 kW。

驗算：
- 8 支都很餓：30/8 = 3.75 kW ✔（驗收 3）
- 2 支降到 0.8 kW：它們 cap = 0.8+0.5 → 以 0.5 kW 級距向上取 = 1.5；其餘 6 支分 (30 − 3)/6 = 4.5 kW；總和 30 kW ✔（驗收 4）

### 4.3 需求偵測與防振盪（hysteresis）

每支樁一個小狀態機，也是純函數 `update_demand(prev, sample, limit) -> DemandState`：

```
         實際功率 < 上限 × 80%，連續 2 筆
 HUNGRY ─────────────────────────────────▶ SATURATED(cap = 實際功率 + 0.5kW, 以 0.5kW 為級距向上取)
   ▲                                              │
   └──────── 實際功率 ≥ cap × 95%（車又要更多） ◀──┘
```

防止反覆調整的 4 道機制：
1. **進入/離開門檻不同**（80% vs 95%）——避免在邊界來回切換。
2. **連續 2 筆才判定**——單筆雜訊不觸發。
3. **cap 以 0.5 kW 級距量化**，SATURATED 狀態下功率小幅波動不會改變 cap，也就不觸發重算。
4. **下達死區**：新上限與目前上限差 < 300 W 的「調升」不送（「調降」一律送，確保安全）。

**觸發重算的事件**：交易開始/結束、樁上下線、需求狀態轉換、SATURATED 的 cap 級距改變。功率小波動不觸發。
**過期資料**：時間戳早於 now − 2 分鐘的讀數（補傳資料）只用於計費，不進需求偵測。

### 4.4 兩階段下達（保證「任何時刻」≤ 30 kW）

```
新分配 → 分成 decreases / increases
  phase 1: 送出所有 decreases，等 ACK，再等 10 秒（樁生效時間，Clock.sleep）
  phase 2: 送出 increases
期間預算以每支 max(舊上限, 新上限) 計算，確保過渡期間也不超標
```
重算由 `PowerManager` 以單一 `asyncio.Lock` 串行化；新的重算請求在進行中時會合併成「之後再算一次」。

### 4.5 斷線與重開機

- WebSocket 關閉或超過 2× heartbeat 沒訊息 → 標記離線，額度保留（4.2 第 1 步）。
- 斷線超過 5 分鐘：狀態不變，**額度持續保留**，其他樁上限不變（驗收 9）。
- 重新連線：先處理補傳佇列，再重算；`BootNotification` 後立即重送該樁上限。
- 樁收到上限前的預設行為：**0 kW（不供電）**，寫入 README 作為通訊約定，模擬器照此實作。

---

## 5. 計量與計費

### 5.1 電量分攤

一筆交易的讀數序列：`(start_ts, meter_start) , samples… , (stop_ts, meter_stop)`。
相鄰兩點之間的電量差，依**時間比例線性分攤**到所跨越的時段（每分鐘一筆通常不跨界；但斷線/重開機造成的缺口可能跨 0:00 或 9:00，所以必須支援切割）。

時段判定 `classify(ts) -> (season, PEAK|OFFPEAK)`：
1. 週六、週日、國定假日 → 離峰
2. 夏月（6/1–9/30）：平日 09:00–24:00 尖峰
3. 非夏月：平日 06:00–11:00、14:00–24:00 尖峰
4. 費率由「該分段所在時間」的季節決定

### 5.2 金額與四捨五入（建議，見第 9 節）

- 每筆交易：各「季節×時段」度數四捨五入到 0.001 度（Decimal，ROUND_HALF_UP）
- 每筆金額 = Σ(度數 × 費率) + 總度數 × 0.5，保留到小數 2 位顯示
- **帳單總額**：各筆未捨入金額加總後四捨五入到元

### 5.3 驗收 6 手算（週五 23:00 → 週六 07:00，假設以 7 kW 定速，非夏月 10 月）

| 時段 | 小時 | 度數 | 費率 | 金額 |
|---|---|---|---|---|
| 週五 23–24 尖峰 | 1 | 7.000 | 4.8 | 33.60 |
| 週六 00–07 離峰 | 7 | 49.000 | 1.9 | 93.10 |
| 管理費 | | 56.000 | 0.5 | 28.00 |
| **合計** | | | | **154.70 → 155 元** |

### 5.4 月結帳單

- 每月 1 日（Scheduler 每小時檢查一次）產生上月帳單；`billing_runs` 確保每期只結算一次。
- **歸屬規則**：帳單 P 包含「已結束、尚未入帳、stop_ts < P 月底」的所有交易。若某筆交易因斷線晚到，自動併入下一期，不會遺漏也不會重複。
- 跨月充電歸屬於**結束時間**所在月份。

---

## 6. OCPP 通訊與可靠性

### 6.1 Frame（OCPP-J 格式）

```
CALL        [2, "<messageId>", "<Action>", {payload}]
CALLRESULT  [3, "<messageId>", {payload}]
CALLERROR   [4, "<messageId>", "<errorCode>", "<desc>", {}]
連線 URL：ws://host/ocpp/{charger_id}
```

各 Action 簡化 payload 詳列於 README，例如：
```json
MeterValues:        {"connectorId":1,"transactionId":42,"timestamp":"2026-10-22T19:01:00+08:00","energyWh":123456,"powerW":3712}
SetChargingProfile: {"connectorId":1,"limitW":3700}
```

### 6.2 冪等（驗收 8）— 兩層去重

1. **訊息層**：`(charger_id, messageId)` 已處理 → 直接回傳快取的 response，不再執行。
2. **語意層**（防止樁重開機後換了 messageId 重送）：
   - `MeterValues`：`UNIQUE(transaction_id, ts)`，重複忽略
   - `StartTransaction`：同樁、同 idTag、同 timestamp → 回傳原 transactionId
   - `StopTransaction`：交易已結束 → 回 ACK，不改資料

### 6.3 已知邊界（列入 README 限制）

- 離線時無法 `Authorize`，故離線期間不得開始新交易（樁本機佇列只會有 MeterValues / StatusNotification / StopTransaction）。
- 以樁的時間戳計費，不處理樁時鐘偏移。
- 永久故障的離線樁會一直佔住保留額度 → 提供管委會「手動釋放」按鈕（待確認）。

---

## 7. 時鐘與模擬器

```python
class Clock(Protocol):
    def now(self) -> datetime: ...          # aware, Asia/Taipei
    async def sleep(self, seconds: float): ...
# SystemClock / FakeClock(手動 advance, 測試用) / ScaledClock(起點 + 倍速)
```

**模擬器**（`chargeshare-sim`，asyncio + websockets client）：
- 每支樁：本機訊息佇列（斷線時累積、重連後依序補送且保留原 messageId）、遵守上限（10 秒內生效）、上限前預設 0 kW。
- EV 模型：電池容量、初始 SOC；SOC 80% 後功率線性降載（「快充滿降載」），加 ±2% 雜訊測防振盪。
- 情境：`normal`、`taper`、`disconnect`（10 分鐘斷線後補傳）、`reboot`（中途重開機，電表不歸零，交易延續）。
- **Recorder**：每支樁把實際送出的電量寫成 JSONL；`chargeshare-sim verify` 與伺服器 DB 比對每筆度數（驗收 10、13）。
- 驗收 13：pytest e2e 在同一 process 內起伺服器 + 模擬器，共享同一個 `ScaledClock`（例如 60 倍速，30 分鐘 ≈ 30 秒）。

---

## 8. 測試規劃（目標 ≥ 45 個）

| 模組 | 預計數量 | 涵蓋驗收 |
|---|---|---|
| allocation / demand | 14 | 3, 4, 5, 9 |
| tariff / metering / billing | 12 | 6, 7, 11 |
| auth_rules + Authorize | 5 | 2 |
| OCPP 去重、補傳、重開機 | 7 | 8, 10 |
| PowerManager（兩階段、斷線保留） | 4 | 9, 12 |
| 月結 / 標記已繳 / Web 登入 | 5 | — |
| e2e 模擬器 | 2 | 10, 13 |

### 開發里程碑（每步可編譯、可測試、可 commit）

| # | 內容 | 驗收 |
|---|---|---|
| M0 | uv 專案骨架、ruff、pytest、Clock、CI | — |
| M1 | domain：tariff + metering + billing | 6, 7, 11 |
| M2 | domain：allocation + demand | 3, 4, 5 |
| M3 | DB models + Alembic + auth_rules | 2 |
| M4 | OCPP WebSocket + TransactionService + 去重 | 8 |
| M5 | PowerManager（兩階段下達、斷線保留） | 9 |
| M6 | 月結帳單 + Scheduler | 11 |
| M7 | Web：管委會儀表板、住戶頁 | 12 |
| M8 | 模擬器 + verify | 10, 13 |
| M9 | 範例資料、README、截圖 | 1 |

---

## 9. 已確認決策（2026-10-22）

| # | 議題 | 決定 |
|---|---|---|
| 1 | 「超過 2 個月未繳」 | 任一張帳單**開立後超過 2 個月**仍未繳 → 拒絕 |
| 2 | 四捨五入 | 每筆交易各時段度數到 0.001 度、金額保留 2 位小數；**帳單總額**才四捨五入到元（ROUND_HALF_UP） |
| 3 | 跨月歸屬 | 依**結束時間**；晚到的交易併入下一期 |
| 4 | 國定假日 | 依人事行政總處行事曆，**含補假日**，以設定檔提供 |
| 5 | 重開機 | 交易**延續**，電表不歸零 |
| 6 | 未收到上限前 | 預設 **0 kW** |
| 7 | 離線額度釋放 | 重新連線時，或管委會**手動釋放** |
| 8 | 工具鏈 | uv + uv.lock、Python 3.13、ruff；Jinja2 + HTMX + Chart.js（vendored） |
| 9 | 儀表板更新 | 每 5 秒**輪詢** |
| 10 | 電表重置（審查回饋） | 同一交易中讀數下降視為重置，分段累加計費；一律收下不回錯誤，標記待管委會確認 |
| 11 | 憑證（審查回饋） | Session 金鑰未設定時自動產生並存於 `data/`；管理員密碼為預設值時拒絕啟動，除非 `--dev` |
