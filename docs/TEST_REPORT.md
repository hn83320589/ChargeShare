# ChargeShare 測試報告

| 項目 | 內容 |
|---|---|
| 報告版本 | 第 2 版（含程式碼審查回饋的修正驗證） |
| 測試日期 | 2026-10-22 |
| 受測版本 | commit `4031b51` |
| 測試環境 | macOS（Apple Silicon）、Python 3.13.17 與 3.12.15、uv 0.13.5、SQLite；GitHub Actions（ubuntu-latest，3.12 / 3.13） |
| 結論 | **14 項驗收標準全部通過；審查指出的 3 個問題皆已修正並有測試** |

---

## 1. 摘要

| 測試類型 | 結果 |
|---|---|
| 自動化測試（Python 3.13，乾淨 clone） | **146 通過 / 0 失敗**，44.3 秒 |
| 自動化測試（Python 3.12，題目最低版本，乾淨 clone） | **146 通過 / 0 失敗**，44.0 秒 |
| 以 pip 安裝（`pip install -e . --group dev`）後執行單元與整合測試 | 通過 |
| Lint / Format（ruff） | 無警告 |
| 鎖定相依版本（`uv lock --check`、`uv sync --locked`） | 一致 |
| 實機驗證（啟動、拒絕不安全設定、偽造 session、WebSocket 授權、30 分鐘模擬、電表重置模擬、儀表板 API 輪詢） | 全部符合預期 |

測試數量分布：

| 層級 | 數量 | 檔案 |
|---|---|---|
| 單元測試（純函數） | 87 | allocation 11、demand 9、dispatch 4、tariff 16、metering 10、billing 5、auth_rules 6、clock 6、ocpp_protocol 6、connections 2、web_helpers 11、simulator_parts 1 |
| 整合測試（DB / WebSocket / 服務 / CLI） | 55 | ocpp_flow 15、power_manager 11、web 11、billing_service 6、db 5、seed 3、authorization 2、cli 1、sim_recorder 1 |
| 端對端測試（伺服器 + 8 支模擬樁） | 4 | simulation 4 |
| **合計** | **146** | |

---

## 2. 驗收標準逐項結果

| # | 驗收標準 | 結果 | 驗證方式與證據 |
|---|---|---|---|
| 1 | 一個指令啟動伺服器並載入範例資料：120 戶、8 支樁、過去 2 個月充電紀錄 | ✅ | 乾淨 clone 後執行 `uv sync --locked` → `uv run chargeshare serve --dev`（本機展示模式，見第 3 節）。資料庫：戶數 120、充電樁 8、充電紀錄 655 筆，期間 2026-08-23 至 2026-10-22（60 天），自動產生 `data/secret_key`（權限 0600）。`/login` 回應 200。自動化：`test_seed.py` 3 項 |
| 2 | 停用卡片、或有超過 2 個月未繳帳單的戶，`Authorize` 被拒絕 | ✅ | 自動化：`test_auth_rules.py` 6 項（含「剛好 2 個月仍接受」邊界、月底日期處理）、`test_authorization.py` 2 項、`test_ocpp_flow.py::test_authorize_over_websocket`。實機以 WebSocket 測試：`RFID-0101-1` Accepted、`RFID-1408-1`（停用）Blocked、`RFID-1507-1`（7 月帳單未繳）Blocked、不存在的卡 Invalid |
| 3 | 8 台同時充電每支 3.75 kW，總和 ≤ 30 kW | ✅ | `test_allocation.py::test_eight_hungry_chargers_share_equally`（純函數）、`test_power_manager.py::test_eight_sessions_get_equal_share`（服務層，實際下達的上限皆為 3,750 W） |
| 4 | 2 台快充滿（< 1 kW）時額度轉給其他 6 台，總和仍 ≤ 30 kW；小幅波動不反覆下達 | ✅ | `test_allocation.py::test_saturated_chargers_release_power_to_others`（2 × 1.5 kW + 6 × 4.5 kW = 30 kW）；`test_demand.py::test_no_repeated_dispatch_under_noise_after_two_cars_taper`（30 分鐘、±2% 雜訊下只調整 1 次）；`test_power_manager.py::test_small_power_fluctuation_sends_nothing`（10 分鐘波動，下達 0 次）、`::test_tapering_cars_release_power_with_decrease_first`（先降後升，間隔 ≥ 10 秒）；另有 5 項需求狀態機遲滯測試 |
| 5 | 22 支同時要充電：最多 21 支供電（29.4 kW），第 22 支排隊；有人結束後依序補上 | ✅ | `test_allocation.py::test_twenty_two_chargers_only_twenty_one_served`、`::test_queued_chargers_are_promoted_in_arrival_order`、`::test_queue_order_follows_arrival_time_not_id`（純函數）；`test_power_manager.py::test_queued_session_is_promoted_when_another_stops`（服務層） |
| 6 | 週五 23:00 → 週六 07:00 尖離峰分攤正確（手算） | ✅ | `test_metering.py::test_friday_night_to_saturday_morning`：尖峰 7.000 度、離峰 49.000 度；`test_billing.py::test_price_friday_night_session`：7 × 4.8 + 49 × 1.9 + 56 × 0.5 = 154.70 元 |
| 7 | 5/31 22:00 → 6/1 08:00 計費正確 | ✅ | `test_metering.py::test_crossing_into_summer_switches_rate_at_midnight`：非夏月離峰 14.000 度、夏月離峰 56.000 度；`test_billing.py::test_price_across_seasons_uses_each_season_rate`：14 × 1.9 + 56 × 2.0 + 70 × 0.5 = 173.60 元 |
| 8 | 斷線補傳資料納入計費；同一訊息重送 3 次度數不變 | ✅ | `test_ocpp_flow.py::test_resending_same_meter_message_three_times_is_counted_once`、`::test_buffered_messages_replayed_after_reconnect_are_billed`、`::test_same_reading_with_new_message_id_is_not_duplicated`、`::test_late_sample_after_stop_reprices_transaction`、`::test_start_transaction_resent_returns_same_transaction`；端對端 `test_ten_minute_disconnect_is_replayed_and_billed` |
| 9 | 斷線 5 分鐘後原分配功率仍保留，其他樁上限不提高 | ✅ | `test_allocation.py::test_offline_charger_keeps_its_reserved_power`；`test_power_manager.py::test_disconnected_charger_keeps_reservation_after_five_minutes`（6 分鐘內未下達任何新上限，保留 3,750 W）、`::test_freed_power_respects_reservation_and_reconnect_resends`、`::test_load_restores_reservation_after_server_restart` |
| 10 | 模擬器「充電中途重新開機」後，度數與模擬器實際送出電量一致 | ✅ | 端對端 `test_reboot_mid_charge_keeps_one_session_with_exact_energy`：CP04 重開機 1 次，伺服器只有 1 筆交易，度數完全相等。**電表歸零的最壞情況**：端對端 `test_meter_reset_on_reboot_is_billed_and_flagged`；實機 `meter-reset` 情境中，CP04 電表由 1,111,220 歸零，伺服器分段累加 588 Wh（220 + 368），與模擬器送出的讀數一致，交易標記待確認 |
| 11 | 月結帳單金額與手算一致（≥ 2 筆、涵蓋尖峰與離峰） | ✅ | `test_billing_service.py::test_monthly_bill_matches_hand_calculation`：154.70 + 26.95 = 181.65 → 182 元，尖峰 10.5 度、離峰 52.5 度；`test_billing.py::test_bill_total_rounds_sum_half_up_to_yuan` |
| 12 | 儀表板總功率曲線即時更新，任何時間點都不超過 30 kW 上限線 | ✅ | 實機（第 1 版）：30 分鐘模擬期間每秒輪詢 `/api/power` 共 30 次，即時點 30 個皆不同；曲線所有點中實際功率最大 29.53 kW、已分配上限（含保留）最大 30.00 kW。本版 30 分鐘模擬：模擬器量測的真實瞬間功率峰值 29.62 kW。自動化：`test_power_manager.py`（以「10 秒內新舊值皆可能生效」計算最壞情況總和 ≤ 30 kW）、`test_web.py::test_power_api_returns_last_hour_with_capacity`、`test_seed.py`。曲線採 monotone 插值，畫面不會因平滑過衝而穿過上限線 |
| 13 | 模擬器 8 支樁 30 分鐘（可加速），伺服器無錯誤，事後比對每筆度數一致 | ✅ | 實機：在乾淨 clone 中執行 `chargeshare simulate --dev --scenario all --minutes 30 --speed 60`，8 筆充電度數與伺服器完全一致，log 中 ERROR 0 筆，exit code 0。自動化：端對端 `test_fleet_of_eight_for_thirty_minutes`（120 倍速，同時驗證無 ERROR 記錄、峰值 ≤ 30 kW） |
| 14 | 測試至少 35 個，`pytest` 全數通過 | ✅ | 146 個，Python 3.12 與 3.13 皆全數通過，GitHub Actions 亦通過 |

---

## 3. 程式碼審查回饋的修正驗證

審查指出 3 個問題，皆先以測試**重現問題（修正前失敗）**，修正後通過。

### 3.1 電表歸零時交易無法結束（影響最大）

| | 修正前 | 修正後 |
|---|---|---|
| 讀數 1,111,213 → 1,111,330 → 4 → 56 → 364，停止 400 | `StopTransaction` 回 `InternalError`；CALLERROR 不快取，樁無限重送；交易停在未結束，度數與金額為空 | 回 `{}`；分段累加 117 + 4 + 52 + 308 + 36 = **517 Wh**；交易標記「電表重置，待管委會確認」 |
| 重置後立刻結束（5,000 → 5,100 → 停止 30） | 同上 | 100 + 30 = **130 Wh** |

- 規則：同一筆交易中讀數下降視為電表重置，該段電量 = 新讀數，之後照常累加。
- 管委會儀表板列出待確認的交易並提供「確認」按鈕；帳單明細標示「電表重置」。
- 測試：`test_ocpp_flow.py::test_meter_reset_mid_charge_is_accumulated_and_flagged`、`::test_meter_reset_right_before_stop_is_accepted`、`test_metering.py` 3 項、`test_web.py::test_admin_confirms_meter_reset`、`::test_bill_detail_marks_meter_reset`、端對端 `test_meter_reset_on_reboot_is_billed_and_flagged`。

### 3.2 公開的預設 session 金鑰可偽造管委會登入

| | 修正前 | 修正後 |
|---|---|---|
| 以 `"dev-only-change-me"` 簽 `{"admin": true}` 的 cookie 存取 `/admin` | **200**，直接進入儀表板（可操作「手動釋放額度」） | **303** 導向登入頁（實機與自動化皆驗證） |
| 未設定 `CHARGESHARE_SECRET_KEY` | 使用公開預設值 | 首次啟動產生隨機金鑰存到 `data/secret_key`（`O_EXCL`、權限 0600），重啟沿用 |
| 管理員密碼為預設值 `admin` | 只記 WARNING，照常啟動 | **拒絕啟動**（CLI 回傳 2，不建立任何資料），除非明確加上 `--dev` |

- 測試：`test_web.py::test_forged_admin_cookie_signed_with_public_string_is_rejected`、`test_web_helpers.py` 3 項（金鑰產生與權限、啟動拒絕、金鑰位置）、`test_cli.py::test_serve_refuses_default_admin_password_without_dev`。
- 順帶修正：環境變數原本在 import 時讀取，改為建立 `Settings` 時讀取。

### 3.3 缺少 LICENSE、只有 uv 安裝方式

- 新增 MIT `LICENSE`，`pyproject.toml` 標註授權。
- README 補上 `pip install -e . --group dev`（pip 25.1 以上），已在乾淨 clone 以 pip 安裝並跑過測試。

### 3.4 修正過程中額外發現

| 問題 | 發現方式 | 修正 |
|---|---|---|
| 啟動時間前 1 分鐘才開始的範例充電沒有中間讀數，批次寫入空串列導致範例資料產生失敗 | 實機以不同時間啟動時發生（原測試固定在 21:30 而未觸發） | 空串列時不寫入；加回歸測試（修正前失敗、修正後通過） |

---

## 4. 交付物檢查

| 交付物 | 狀態 | 位置 |
|---|---|---|
| 原始碼 | ✅ | `src/chargeshare/` |
| `pyproject.toml` 與鎖定的相依版本 | ✅ | `pyproject.toml`、`uv.lock` |
| README：啟動方式（uv 與 pip）、模擬器使用方式 | ✅ | README「快速開始」「模擬器」 |
| README：WebSocket 訊息格式 | ✅ | README「WebSocket 訊息格式」 |
| README：功率分配演算法與防止反覆調整 | ✅ | README「功率分配演算法」，含先降後升時序圖與最壞情況對照圖 |
| README：計費與分攤規則 | ✅ | README「計費與分攤規則」 |
| README：斷線與補傳的處理方式 | ✅ | README「斷線與補傳」，含電表重置 |
| README：系統架構圖 | ✅ | README「系統架構」（Mermaid） |
| README：畫面截圖（管委會儀表板、住戶帳單） | ✅ | `docs/screenshots/` 共 4 張 |
| README：已知限制 | ✅ | README「已知限制」 |
| 範例資料產生腳本 | ✅ | `src/chargeshare/seed.py`（`chargeshare serve` 首次啟動時執行） |
| 費率設定檔 | ✅ | `config/tariff.toml` |
| 2026 年國定假日資料 | ✅ | `config/holidays_2026.toml`（依人事行政總處 115 年辦公日曆表，含補假；104 個週末 + 16 個平日假日 = 120 天，與官方公布總數相符） |
| pytest 測試 | ✅ | `tests/`（146 個） |
| 加分項：GitHub Actions CI | ✅ | `.github/workflows/ci.yml`（Python 3.12 / 3.13 矩陣：ruff + pytest），README 有 CI badge |
| 授權 | ✅ | `LICENSE`（MIT） |

---

## 5. 實機驗證步驟（可重現）

```bash
# 1. 乾淨環境安裝並跑測試
git clone https://github.com/hn83320589/ChargeShare && cd ChargeShare
uv sync --locked
uv run pytest

# 2. 一個指令啟動（驗收 1；本機展示需加 --dev，正式部署改設 CHARGESHARE_ADMIN_PASSWORD）
uv run chargeshare serve --dev
#   → http://127.0.0.1:8000/admin（admin / admin）
#   → http://127.0.0.1:8000/login（01-01 / pw-01-01）

# 3. 30 分鐘加速模擬並自動比對（驗收 10、12、13）
uv run chargeshare simulate --dev --scenario all --minutes 30 --speed 60

# 4. 電表歸零的最壞情況
uv run chargeshare simulate --dev --scenario meter-reset --minutes 12 --speed 60
```

本版實機 30 分鐘模擬的輸出：

```text
CP01 tx 656：0.984 度（重開機 0 次、斷線 0 次、電表重置 0 次）   ← 快充滿降載
CP02 tx 657：0.983 度（重開機 0 次、斷線 0 次、電表重置 0 次）   ← 快充滿降載
CP03 tx 658：1.999 度（重開機 0 次、斷線 1 次、電表重置 0 次）   ← 斷線 10 分鐘後補傳
CP04 tx 659：1.889 度（重開機 1 次、斷線 0 次、電表重置 0 次）   ← 充電中途重開機
CP05 tx 660：1.903 度（重開機 0 次、斷線 0 次、電表重置 0 次）
CP06 tx 661：1.871 度（重開機 0 次、斷線 0 次、電表重置 0 次）
CP07 tx 662：1.832 度（重開機 0 次、斷線 0 次、電表重置 0 次）
CP08 tx 663：1.822 度（重開機 0 次、斷線 0 次、電表重置 0 次）
模擬期間實際總功率峰值：29.62 kW
比對完成：8 筆充電的度數與伺服器完全一致
```

---

## 6. 開發過程中由測試發現並已修正的問題（第 1 版）

| 問題 | 發現方式 | 修正 |
|---|---|---|
| 分配結果不均（3.7 / 3.8 kW），捨去的零頭在樁之間挪動，額外觸發一次調整 | 單元測試（驗收 3、防振盪測試） | 改為先找出共同水位、再一次捨去；級距由 100 W 改為 50 W |
| 逾時偵測關閉連線後，不會標記離線也不會發布事件 | 程式審查 | 偵測只負責關閉 socket，由連線迴圈統一收尾 |
| 模擬樁上限不依序套用，峰值 30.1 kW | 檢視端對端實際數據（測試偶然通過） | 依收到順序套用並以序號保護；修正後峰值穩定在約 29.6 kW |
| 範例資料少產生「今天」，最近 1 小時曲線空白 | 截圖時發現 | 修正迴圈邊界，加回歸測試 |
| 管理員密碼比對非固定時間 | 背景安全檢查 | 改用 `hmac.compare_digest`（預設憑證問題於第 2 版徹底修正，見 3.2） |

---

## 7. 已知事項

- **測試警告 1 則**：Starlette 提示 TestClient 未來改用 `httpx2` 套件。屬於上游的棄用提示，不影響功能；更換套件需另行決定。
- **「排隊中」狀態**：實體只有 8 支樁，8 × 1.4 kW < 30 kW，正常運作不會排隊，所以只以純函數與服務層測試驗證（驗收 5 本身即要求以純函數測試）。
- **電表重置的電量缺口**：重置前最後一筆讀數到實際歸零之間的電量（最多 1 分鐘，且重開機期間不供電）伺服器無從得知，不計入；這類交易會標記給管委會確認。
- **時序相關的端對端測試**在 120 倍速下執行；本機與 CI 皆穩定通過。若 CI 機器負載過高，可降低倍速。
- 其餘系統限制見 README「已知限制」。
