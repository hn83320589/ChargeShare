# 第 9 章　測試策略：讓測試證明系統是對的

## 學習目標

- 依測試金字塔分配測試，並知道每一層該測什麼
- 實踐 TDD 的 red → green，以及**確認 red 是真的 red**
- 寫手算測試、邊界測試、不變量測試
- 為時序與安全性設計專門的檢查工具（最壞情況計算）
- 讓模擬器當「裁判」，做端對端驗證
- 正確處理偶發失敗（flaky test）

---

## 9.1 測試金字塔

| 層級 | 數量 | 速度 | 測什麼 |
|---|---|---|---|
| 單元（`tests/unit`） | 90 | 全部 < 1 秒 | domain 的純函數、協定解析、時鐘 |
| 整合（`tests/integration`） | 55 | 約 4 秒 | 資料庫、WebSocket 流程、PowerManager、網頁、CLI |
| 端對端（`tests/e2e`） | 4 | 約 38 秒 | 真實伺服器 + 8 支模擬樁 |

分層的原則：**能在下層測的就不要放到上層**。注水法的 11 個案例全部在單元層，
因為純函數不需要資料庫；但「重送 3 次只算一次」必須在整合層，因為去重靠的是資料庫約束與交易。

第 3 章把演算法寫成純函數，就是為了讓大部分測試能落在又快又穩的單元層。

---

## 9.2 TDD：先看到它失敗

本專案每個功能都先寫測試、確認失敗（red）、再實作（green）。例如月結帳單：

```python
def test_monthly_bill_matches_hand_calculation(session, tariff, households):
    # 驗收 11：
    #   第 1 筆 10/23（五）23:00 → 10/24（六）07:00，7 kW：尖峰 7 度、離峰 49 度 → 154.70
    #   第 2 筆 10/22（四）10:00 → 12:00，3.5 kW：尖峰 3.5 度、離峰 3.5 度 → 26.95
    #   合計 181.65 → 182 元
    ...
    assert bill.total_amount == 182
```

### 確認 red 是「對的 red」

測試失敗的原因要是「功能還沒做」，而不是「測試寫錯」。本專案有一次差點被騙：
想確認範例資料的回歸測試在修正前會失敗，用 `git stash` 暫存修正，結果整段功能都被 stash 掉，
測試因為 **import 錯誤**而失敗——那不算證明。後來改成只把修正的那一行暫時改回去，看到 `assert 0 > 0` 才確認。

> 💡 **教學重點**：看到 red 時，讀一下失敗訊息。「AttributeError: module has no attribute」
> 和「assert 0 > 0」是完全不同的 red。

---

## 9.3 手算測試

計費的測試直接把手算過程寫在註解裡，讓審閱者可以獨立驗算：

```python
def test_price_across_seasons_uses_each_season_rate(tariff):
    # 14 度 × 1.9 + 56 度 × 2.0 + 70 度 × 0.5 = 26.6 + 112 + 35 = 173.6
    charge = price({NON_OFF: 14000, SUMMER_OFF: 56000}, tariff)
    assert charge.amount == Decimal("173.6")
```

手算測試的價值是**獨立性**：測試的期望值不是由被測程式算出來的。
如果用程式算期望值（例如在測試裡再寫一次分攤邏輯），兩邊有同樣的 bug 時測試照樣通過。

---

## 9.4 邊界測試

錯誤集中在邊界。用 `pytest.mark.parametrize` 把邊界一次列清楚：

```python
@pytest.mark.parametrize(("when", "expected"), [
    (tw(2026, 7, 1, 8, 59), SUMMER_OFF),     # 尖峰前一分鐘
    (tw(2026, 7, 1, 9, 0), SUMMER_PEAK),     # 尖峰第一分鐘
    (tw(2026, 7, 1, 23, 59), SUMMER_PEAK),
    (tw(2026, 7, 2, 0, 0), SUMMER_OFF),      # 24:00 後回到離峰
    (tw(2026, 10, 22, 11, 0), NON_OFF),      # 非夏月午間離峰
    (tw(2026, 10, 24, 12, 0), NON_OFF),      # 週六
])
def test_period_at_weekday_and_weekend(tariff, when, expected):
    assert tariff.period_at(when) == expected
```

其他例子：

- 授權：「剛好 2 個月」仍接受、「超過 2 個月」拒絕；`12/31 + 2 個月 = 2/28`。
- 季節：5/31 23:59 非夏月、6/1 00:00 夏月、9/30 23:59 夏月、10/1 00:00 非夏月。

---

## 9.5 不變量測試：讓電腦想案例

手寫案例只能測到你想得到的情況。**不變量測試**換個角度：不管輸入是什麼，某些性質一定成立。

```python
def test_invariants_hold_for_random_inputs():
    rng = random.Random(20261022)          # 固定種子：失敗時可重現
    for _ in range(2_000):
        chargers = [hungry(f"CP{i:02d}",
                           minute=rng.randint(0, 120),
                           online=rng.random() > 0.15,
                           has_session=rng.random() > 0.2,
                           demand_cap_w=rng.choice([7_000, rng.randint(0, 7_000)]),
                           held_limit_w=rng.choice([0, 1_400, 3_750, 7_000]))
                    for i in range(rng.randint(0, 30))]
        result = allocate(chargers, CONFIG)
        served = [w for w in result.limits.values() if w > 0]
        budget = max(CONFIG.capacity_w - sum(result.reserved.values()), 0)
        assert sum(served) <= budget                                   # 不超過預算
        assert all(CONFIG.min_w <= w <= CONFIG.max_w for w in served)  # 每支在範圍內
        assert all(w % CONFIG.step_w == 0 for w in served)             # 是級距的倍數
        if result.queued:
            assert len(served) == budget // CONFIG.min_w               # 有人排隊時名額用滿
```

這個測試第一次跑就失敗了，抓到一個值得思考的情況：隨機產生的離線樁保留額度加總超過 30 kW。
實務上不會發生（保留額度來自過去的合法分配），但它讓我們把不變量**說得更精確**：
不是「總和 ≤ 30 kW」，而是「線上分配 ≤ 30 kW − 保留」。**不變量測試會逼你把規格想清楚。**

（若想更進一步，可以用 Hypothesis 這類 property-based testing 套件，它還會自動縮小失敗案例。）

---

## 9.6 為安全性寫專用的檢查工具

「任何時刻 ≤ 30 kW」不能只看最終狀態，因為問題出在過渡期間。測試輔助函式 `worst_case_peak()`
重建每一支樁上限的時間軸，以「下達後 10 秒內新舊值都可能生效，取較大者」計算每個時間點的最壞總和：

```python
# tests/integration/power_helpers.py
def worst_case_peak(sends: list[tuple[datetime, str, int]]) -> int:
    """樁收到新上限後 10 秒內才生效：這段期間舊值與新值都可能在用，取較大者。"""
    ...
    checkpoints = {ts for ts, _, _ in sends} | {ts + SETTLE for ts, _, _ in sends}

    def upper(charger_id: str, at: datetime) -> int:
        value = 0
        for ts, limit in history[charger_id]:
            if ts > at:
                break
            previous = value
            value = limit if at >= ts + SETTLE else max(previous, limit)
        return value

    return max((sum(upper(c, t) for c in history) for t in checkpoints), default=0)
```

搭配假的連線 `FakeLink`（記錄每次下達的時間與數值），PowerManager 的每個測試最後都斷言：

```python
assert worst_case_peak(link.sends) <= 30_000
```

只需要檢查 checkpoints（每次下達的時刻與 10 秒後），因為最壞總和只會在這些時刻改變。

> 💡 **教學重點**：對於「任何時刻」「從不」這類性質，要把**時間軸**當成測試的對象，
> 而不是只看某個時間點的快照。

---

## 9.7 端對端：讓模擬器當裁判

端對端測試讓模擬器扮演三種角色：

1. **負載產生器**：8 支樁、電池模型、快充滿降載、斷線、重開機、電表歸零。
2. **記錄者**：記下實際送出的每筆讀數，依同樣的規則累加應計費的電量。
3. **量測者**：`FleetMonitor` 在每次功率變化時加總**真實瞬間功率**，記錄峰值。

測試最後比對：

```python
async def test_fleet_of_eight_for_thirty_minutes(tmp_path, caplog):
    result, problems, _ = await simulate(tmp_path, "all", minutes=30)
    assert len(result.recorder.sessions) == 8
    assert problems == []                       # 每筆度數與伺服器一致
    assert result.monitor.peak_w <= 30_000      # 真實功率從未超過上限
    assert server_errors(caplog) == []          # 沒有任何 ERROR 等級的 log
```

最後一行看起來很嚴格，卻是最有價值的一行：它抓到了「Future exception was never retrieved」
這種**功能結果正確、但系統有隱藏問題**的狀況（第 10 章案例 8）。

### 量測真實值，不要只檢查自己的計算

伺服器「分配的上限」加總 ≤ 30 kW 是一回事，樁「實際用的功率」≤ 30 kW 是另一回事。
模擬器的峰值量測抓到了一個伺服器分配正確、但模擬樁執行錯誤導致 30.1 kW 的 bug（第 10 章案例 3）。
**驗證要盡量靠近真實的結果**。

---

## 9.8 不只看測試結果，也看數據

端對端測試第一次就全部通過。這時候反而要懷疑：測試真的有測到東西嗎？

本專案在測試通過後，另外寫了一個腳本印出每筆充電的度數、樣本數、峰值，結果發現峰值是 30,109 W——
測試會通過只是因為那次的隨機時序剛好沒踩到。**一次就過的測試要多看一眼**。

---

## 9.9 偶發失敗（Flaky Test）

推上 GitHub 後，CI 偶發失敗了兩次，本機卻從未重現。錯誤的處理方式：

- ❌ 加 `@pytest.mark.flaky(reruns=3)` 自動重跑
- ❌ 把逾時時間調大
- ❌ 把嚴格的斷言拿掉

正確的處理方式：**偶發失敗是真正的 bug，只是觸發條件比較少見**。

1. 從失敗訊息找到根因（未讀取的 Future、故障時間以絕對時間計算）。
2. 寫一個**確定會失敗**的測試重現它（用假 socket 在 `send()` 時中斷連線；用 FakeClock 讓充電晚開始）。
3. 修正，看到測試轉綠。
4. 用壓力驗證：本機 24 份 e2e 並行（共 96 次）、CI 連跑 3 次。

第一個問題還順便找到伺服器端同樣的 bug——如果當初選擇「自動重跑」，這個正式環境的問題就被掩蓋了。

---

## 9.10 其他實用技巧

| 技巧 | 例子 |
|---|---|
| migration 與 model 一致性 | `test_migrations_match_models` |
| 固定隨機種子 | `random.Random(20261022)`，失敗可重現 |
| 測試中的成本控制 | 密碼雜湊可注入（`seed_base(session, hasher=...)`），測試不必花 6 秒算 120 個 scrypt |
| 在多個 Python 版本跑 | CI 矩陣 3.12 / 3.13；本機用 `uv run --python 3.12` |
| 從乾淨 clone 跑一次 | 確認沒有漏提交的檔案 |
| 驗收對照表 | README 列出每項驗收對應的測試 |

---

## 小結

1. 能在下層測的不放上層；架構決定了測試能不能下沉。
2. 確認 red 的原因是「功能還沒做」。
3. 手算測試的期望值要獨立於被測程式。
4. 不變量測試讓電腦想案例，也逼你把規格說清楚。
5. 「任何時刻」的性質要檢查整條時間軸。
6. 端對端測試量測真實結果，並拒絕任何 ERROR log。
7. 偶發失敗是 bug：重現、修正、壓力驗證，不要自動重跑。

## 思考題

1. 為 `_round_preserving_total()` 寫一個不變量測試：隨機產生分數，檢查哪些性質？
2. `worst_case_peak()` 本身也是程式，它可能有 bug 嗎？你會怎麼測試這個測試工具？
3. 端對端測試在 120 倍速下跑。列出 3 個在高倍速下可能被掩蓋、在真實速度下才會出現的問題。
