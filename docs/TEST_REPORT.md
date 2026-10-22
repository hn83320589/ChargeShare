# ChargeShare 測試報告

| 項目 | 內容 |
|---|---|
| 測試日期 | 2026-10-22 |
| 受測版本 | commit `75ca5b2`（M9） |
| 測試環境 | macOS（Apple Silicon）、Python 3.13.17 與 3.12.15、uv 0.13.5、SQLite |
| 結論 | **14 項驗收標準全部通過** |

---

## 1. 摘要

| 測試類型 | 結果 |
|---|---|
| 自動化測試（Python 3.13） | **133 通過 / 0 失敗**，36.9 秒 |
| 自動化測試（Python 3.12，題目最低版本） | **133 通過 / 0 失敗**，37.5 秒 |
| 端對端測試穩定性（再連續重跑 3 次） | 3/3、3/3、3/3 通過 |
| 乾淨 clone 安裝後執行測試 | 133 通過（確認所有必要檔案皆已提交） |
| Lint / Format（ruff） | 無警告 |
| 鎖定相依版本（`uv lock --check`、`uv sync --locked`） | 一致 |
| 實機驗證（啟動伺服器、WebSocket 授權、30 分鐘模擬、儀表板 API 輪詢） | 全部符合預期 |

測試數量分布：

| 層級 | 數量 | 檔案 |
|---|---|---|
| 單元測試（純函數） | 83 | allocation 11、demand 9、dispatch 4、tariff 16、metering 8、billing 5、auth_rules 6、clock 6、ocpp_protocol 6、connections 2、web_helpers 9、simulator_parts 1 |
| 整合測試（DB / WebSocket / 服務） | 47 | ocpp_flow 13、power_manager 11、web 7、billing_service 6、db 5、authorization 2、seed 2、sim_recorder 1 |
| 端對端測試（伺服器 + 8 支模擬樁） | 3 | simulation 3 |
| **合計** | **133** | |

---

## 2. 驗收標準逐項結果

| # | 驗收標準 | 結果 | 驗證方式與證據 |
|---|---|---|---|
| 1 | 一個指令啟動伺服器並載入範例資料：120 戶、8 支樁、過去 2 個月充電紀錄 | ✅ | 乾淨 clone 後執行 `uv sync --locked` → `uv run chargeshare serve`。資料庫：戶數 120、充電樁 8、卡片 150、充電紀錄 653 筆、讀數 127,004 筆，期間 2026-08-23 至 2026-10-22（60 天），7–9 月帳單 171 張。`/login`、`/admin` 回應 200。自動化：`test_seed.py` 2 項 |
| 2 | 停用卡片、或有超過 2 個月未繳帳單的戶，`Authorize` 被拒絕 | ✅ | 自動化：`test_auth_rules.py` 6 項（含「剛好 2 個月仍接受」邊界、月底日期處理）、`test_authorization.py` 2 項、`test_ocpp_flow.py::test_authorize_over_websocket`。實機以 WebSocket 測試：`RFID-0101-1` Accepted、`RFID-1408-1`（停用）Blocked、`RFID-1507-1`（7 月帳單未繳）Blocked、不存在的卡 Invalid |
| 3 | 8 台同時充電每支 3.75 kW，總和 ≤ 30 kW | ✅ | `test_allocation.py::test_eight_hungry_chargers_share_equally`（純函數）、`test_power_manager.py::test_eight_sessions_get_equal_share`（服務層，實際下達的上限皆為 3,750 W） |
| 4 | 2 台快充滿（< 1 kW）時額度轉給其他 6 台，總和仍 ≤ 30 kW；小幅波動不反覆下達 | ✅ | `test_allocation.py::test_saturated_chargers_release_power_to_others`（2 × 1.5 kW + 6 × 4.5 kW = 30 kW）；`test_demand.py::test_no_repeated_dispatch_under_noise_after_two_cars_taper`（30 分鐘、±2% 雜訊下只調整 1 次）；`test_power_manager.py::test_small_power_fluctuation_sends_nothing`（10 分鐘波動，下達 0 次）、`::test_tapering_cars_release_power_with_decrease_first`（先降後升，間隔 ≥ 10 秒）；另有 5 項需求狀態機遲滯測試 |
| 5 | 22 支同時要充電：最多 21 支供電（29.4 kW），第 22 支排隊；有人結束後依序補上 | ✅ | `test_allocation.py::test_twenty_two_chargers_only_twenty_one_served`、`::test_queued_chargers_are_promoted_in_arrival_order`、`::test_queue_order_follows_arrival_time_not_id`（純函數）；`test_power_manager.py::test_queued_session_is_promoted_when_another_stops`（服務層） |
| 6 | 週五 23:00 → 週六 07:00 尖離峰分攤正確（手算） | ✅ | `test_metering.py::test_friday_night_to_saturday_morning`：尖峰 7.000 度、離峰 49.000 度；`test_billing.py::test_price_friday_night_session`：7 × 4.8 + 49 × 1.9 + 56 × 0.5 = 154.70 元 |
| 7 | 5/31 22:00 → 6/1 08:00 計費正確 | ✅ | `test_metering.py::test_crossing_into_summer_switches_rate_at_midnight`：非夏月離峰 14.000 度、夏月離峰 56.000 度；`test_billing.py::test_price_across_seasons_uses_each_season_rate`：14 × 1.9 + 56 × 2.0 + 70 × 0.5 = 173.60 元 |
| 8 | 斷線補傳資料納入計費；同一訊息重送 3 次度數不變 | ✅ | `test_ocpp_flow.py::test_resending_same_meter_message_three_times_is_counted_once`、`::test_buffered_messages_replayed_after_reconnect_are_billed`、`::test_same_reading_with_new_message_id_is_not_duplicated`、`::test_late_sample_after_stop_reprices_transaction`、`::test_start_transaction_resent_returns_same_transaction`；端對端 `test_ten_minute_disconnect_is_replayed_and_billed` |
| 9 | 斷線 5 分鐘後原分配功率仍保留，其他樁上限不提高 | ✅ | `test_allocation.py::test_offline_charger_keeps_its_reserved_power`；`test_power_manager.py::test_disconnected_charger_keeps_reservation_after_five_minutes`（6 分鐘內未下達任何新上限，保留 3,750 W）、`::test_freed_power_respects_reservation_and_reconnect_resends`、`::test_load_restores_reservation_after_server_restart` |
| 10 | 模擬器「充電中途重新開機」後，度數與模擬器實際送出電量一致 | ✅ | 端對端 `test_reboot_mid_charge_keeps_one_session_with_exact_energy`：CP04 重開機 1 次，伺服器只有 1 筆交易，度數完全相等。實機 30 分鐘模擬：CP04 重開機 1 次，1.896 度，比對一致 |
| 11 | 月結帳單金額與手算一致（≥ 2 筆、涵蓋尖峰與離峰） | ✅ | `test_billing_service.py::test_monthly_bill_matches_hand_calculation`：154.70 + 26.95 = 181.65 → 182 元，尖峰 10.5 度、離峰 52.5 度；`test_billing.py::test_bill_total_rounds_sum_half_up_to_yuan` |
| 12 | 儀表板總功率曲線即時更新，任何時間點都不超過 30 kW 上限線 | ✅ | 實機：30 分鐘模擬期間每秒輪詢 `/api/power` 共 30 次，**即時點 30 個皆不同**；曲線所有點中實際功率最大 29.53 kW、已分配上限（含保留）最大 30.00 kW。模擬器量測的真實瞬間功率峰值 29.63 kW。自動化：`test_power_manager.py`（以「10 秒內新舊值皆可能生效」計算最壞情況總和 ≤ 30 kW）、`test_web.py::test_power_api_returns_last_hour_with_capacity`、`test_seed.py`。曲線採 monotone 插值，畫面不會因平滑過衝而穿過上限線 |
| 13 | 模擬器 8 支樁 30 分鐘（可加速），伺服器無錯誤，事後比對每筆度數一致 | ✅ | 實機：在乾淨 clone 中執行 `chargeshare simulate --scenario all --minutes 30 --speed 60`，8 筆充電度數與伺服器完全一致，log 中 ERROR 0 筆，exit code 0。自動化：端對端 `test_fleet_of_eight_for_thirty_minutes`（120 倍速，同時驗證無 ERROR 記錄、峰值 ≤ 30 kW） |
| 14 | 測試至少 35 個，`pytest` 全數通過 | ✅ | 133 個，Python 3.12 與 3.13 皆全數通過 |

---

## 3. 交付物檢查

| 交付物 | 狀態 | 位置 |
|---|---|---|
| 原始碼 | ✅ | `src/chargeshare/` |
| `pyproject.toml` 與鎖定的相依版本 | ✅ | `pyproject.toml`、`uv.lock` |
| README：啟動方式、模擬器使用方式 | ✅ | README「快速開始」「模擬器」 |
| README：WebSocket 訊息格式 | ✅ | README「WebSocket 訊息格式」 |
| README：功率分配演算法與防止反覆調整 | ✅ | README「功率分配演算法」 |
| README：計費與分攤規則 | ✅ | README「計費與分攤規則」 |
| README：斷線與補傳的處理方式 | ✅ | README「斷線與補傳」 |
| README：系統架構圖 | ✅ | README「系統架構」（Mermaid） |
| README：畫面截圖（管委會儀表板、住戶帳單） | ✅ | `docs/screenshots/` 共 4 張 |
| README：已知限制 | ✅ | README「已知限制」 |
| 範例資料產生腳本 | ✅ | `src/chargeshare/seed.py`（`chargeshare serve` 首次啟動時執行） |
| 費率設定檔 | ✅ | `config/tariff.toml` |
| 2026 年國定假日資料 | ✅ | `config/holidays_2026.toml`（依人事行政總處 115 年辦公日曆表，含補假；104 個週末 + 16 個平日假日 = 120 天，與官方公布總數相符） |
| pytest 測試 | ✅ | `tests/`（133 個） |
| 加分項：GitHub Actions CI | ✅ | `.github/workflows/ci.yml`（Python 3.12 / 3.13 矩陣：ruff + pytest） |

---

## 4. 實機驗證步驟（可重現）

```bash
# 1. 乾淨環境安裝並跑測試
git clone <repo> && cd ChargeShare
uv sync --locked
uv run pytest

# 2. 一個指令啟動（驗收 1）
uv run chargeshare serve
#   → http://127.0.0.1:8000/admin（admin / admin）
#   → http://127.0.0.1:8000/login（01-01 / pw-01-01）

# 3. 30 分鐘加速模擬並自動比對（驗收 10、12、13）
uv run chargeshare simulate --scenario all --minutes 30 --speed 60
```

實機 30 分鐘模擬的輸出：

```text
CP01 tx 654：0.983 度（重開機 0 次、斷線 0 次）   ← 快充滿降載
CP02 tx 655：0.983 度（重開機 0 次、斷線 0 次）   ← 快充滿降載
CP03 tx 656：2.004 度（重開機 0 次、斷線 1 次）   ← 斷線 10 分鐘後補傳
CP04 tx 657：1.896 度（重開機 1 次、斷線 0 次）   ← 充電中途重開機
CP05 tx 658：1.910 度（重開機 0 次、斷線 0 次）
CP06 tx 659：1.877 度（重開機 0 次、斷線 0 次）
CP07 tx 660：1.839 度（重開機 0 次、斷線 0 次）
CP08 tx 661：1.828 度（重開機 0 次、斷線 0 次）
模擬期間實際總功率峰值：29.63 kW
比對完成：8 筆充電的度數與伺服器完全一致
```

---

## 5. 開發過程中由測試發現並已修正的問題

| 問題 | 發現方式 | 修正 |
|---|---|---|
| 分配結果不均（3.7 / 3.8 kW），捨去的零頭在樁之間挪動，額外觸發一次調整 | 單元測試（驗收 3、防振盪測試） | 改為先找出共同水位、再一次捨去；級距由 100 W 改為 50 W |
| 逾時偵測關閉連線後，不會標記離線也不會發布事件 | 程式審查 | 偵測只負責關閉 socket，由連線迴圈統一收尾 |
| 模擬樁上限不依序套用，峰值 30.1 kW | 檢視端對端實際數據（測試偶然通過） | 依收到順序套用並以序號保護；修正後峰值穩定在約 29.6 kW |
| 範例資料少產生「今天」，最近 1 小時曲線空白 | 截圖時發現 | 修正迴圈邊界，加回歸測試（已確認修正前失敗、修正後通過） |
| 管理員密碼比對非固定時間；預設憑證 | 背景安全檢查 | 改用 `hmac.compare_digest`；使用預設值時啟動即記錄 WARNING |

---

## 6. 已知事項

- **測試警告 1 則**：Starlette 提示 TestClient 未來改用 `httpx2` 套件。屬於上游的棄用提示，不影響功能；更換套件需另行決定。
- **「排隊中」狀態**：實體只有 8 支樁，8 × 1.4 kW < 30 kW，正常運作不會排隊，所以只以純函數與服務層測試驗證（驗收 5 本身即要求以純函數測試）。
- **時序相關的端對端測試**在 120 倍速下執行；本次連續 4 輪皆通過。若 CI 機器負載過高，可降低倍速。
- 其餘系統限制見 README「已知限制」。
