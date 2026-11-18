# 工作流 B 介面說明（鍾易憲）

成功與失敗範例都在 [`examples/`](./examples/)，由 `python -m docs.interfaces.make_examples` 從程式實際輸出產生。改欄位時請同步更新本文件，並重新產生範例。

## 0. 共用慣例（第 1 週，待 A、C 確認）

定義在 `src/common/schema.py`。

| 項目 | 約定 |
| --- | --- |
| 時間 | ISO 8601，含時區，統一 `+08:00`（Asia/Taipei）。沒有時區的字串視為 +08:00。 |
| 室內房間 | `room_id`，例如 `room1` |
| 外出地點 | 縣市＋行政區，例如 `臺中市`／`南區`；「台」會自動轉成「臺」 |
| 單位 | 溫度 °C（欄位 `_c`）、相對濕度 %（欄位 `_pct`）、降雨機率 %（`pop_pct`） |
| 缺值 | `null`，不填 0、-1 或推測值 |
| 狀態 `status` | `ok`、`missing`（來源沒有）、`stale`（過期）、`unknown`（無法判定）、`error`（取得失敗） |
| 操作標記 | `ac`、`dehumidifier`、`window`、`mixed`、`none_confirmed`、`unknown` |

## 1. 預報工具 `get_forecast`（給 C）

```python
from src.acquisition.cwa_forecast import get_forecast, forecast_from_snapshot
r = get_forecast("臺中市", "南區", "2026-11-19T09:00", "2026-11-19T15:00")  # 讀 CWA_API_KEY
```

不會丟例外，結果看 `status`：

| status | 情況 | C 的建議處理 | 範例 |
| --- | --- | --- | --- |
| `ok` | 取得成功 | 顯示 `values`、`location`、`target`、`retrieved_at`、`source` | `forecast_ok.json` |
| `missing` | 時段超出預報範圍、時段不完整、找不到行政區、不支援的縣市 | 請使用者補充或換時段 | `forecast_missing_time.json` |
| `stale` | 快照超過 6 小時（`check_freshness`） | 說明資料時間，或重新查詢 | `forecast_stale.json` |
| `error` | 沒設金鑰、連線失敗、HTTP 錯誤、非 JSON、`success=false` | 說明暫時無法取得預報，不給肯定建議 | `forecast_error_api.json` |

重要欄位：`values.temperature_c{min,max,mean}`、`values.relative_humidity_pct{min,max,mean}`、`values.pop_pct{max}`、`values.weather[]`、`slots_used[]`（實際用到的預報時段）、`valid_from`／`valid_to`（整份預報涵蓋範圍）、`issued_at`（來源沒提供就是 `null`）、`missing_fields`（目標時段缺哪些欄位）。

點預報（溫度、濕度，每 3 小時一個時間點）取落在目標時段內的點；區間預報（降雨機率、天氣現象）取和目標時段重疊的區間。

## 2. 室內量測（A 提供，B 讀取）

B 需要的欄位見 `measurements_input_from_A.json`：`room_id, measured_at, temperature_c, relative_humidity_pct, quality_flag`。

- `src/analytics/quality.py::load_measurements_sqlite()` 依 DESIGN.md §4.2 草案的 `measurements` 表讀取。A 的查詢介面定案後，改成呼叫 A 的函式即可，後面的流程不變。
- `indoor_summary(rows, now)` 回傳最新讀值、資料年齡、近 30 分鐘變化（`indoor_summary.json`），給規則與 C 的畫面使用。新鮮度預設 10 分鐘。

## 3. 事件判定 `detect_events`（給 C 和 A）

```python
from src.analytics.events import detect_events
r = detect_events(rows, "room1")   # → {events: [...], quality: {...}, config: {...}}
```

每個事件（`event_detected.json`）：

| 欄位 | 說明 |
| --- | --- |
| `event_id` | `ev-<room>-<開始時間>`，詢問、回覆、操作時間都用這個 id 串起來 |
| `start_at` / `decision_at` / `end_at` | 變化開始／可以判斷是事件的時間／結束時間 |
| `features` | 只用 `decision_at` 以前的資料，分類用 |
| `summary` | 事件結束後的總變化，只供顯示，不進入分類 |
| `n_runs` | 合併了幾次候選偵測 |
| `event_version` | 偵測器版本＋門檻設定版本 |

單點或兩點跳動、斷線恢復後的落差都不會產生事件（`event_none_spike.json`）。

## 4. 詢問間隔 `should_ask`（給 C）

```python
from src.analytics.ask_policy import should_ask
d = should_ask(event, last_asked_at)   # 同房間上一次詢問時間，沒有就傳 None
```

`ask=false` 時 `ask_status = suppressed_cooldown`，事件照樣保存，回覆維持 `unknown`（`ask_decision.json`）。預設間隔 30 分鐘。

## 5. 建議候選 `recommend`（給 C）

```python
from src.knowledge.rules import recommend
c = recommend("device" | "outing" | "laundry", indoor=..., forecast=..., device_state=..., preference=...)
```

| status | 意義 | C 的處理 |
| --- | --- | --- |
| `ok` | 規則已套用 | 依 `rules_fired[].suggestion` 與 `evidence` 產生回答；沒有命中規則時看 `note` |
| `partial` | 曬衣只有一側資料 | 只說可用的一側，並顯示 `limitations` |
| `needs_clarification` | 缺地點或時段 | 顯示 `clarify` |
| `insufficient_data` | 資料過期、失敗或缺少 | 顯示 `missing`，不給肯定建議 |

`device_state` 例如 `{"ac": "on|off|unknown", "dehumidifier": "...", "confirmed_at": "..."}`；沒傳就當作 `unknown`。
生成的回答若和 `evidence` 的數值、時間、地點不一致，請改用模板（DESIGN.md Algorithm 1 第 13–14 行）。

規則表在 `RULES`，每條都有 `basis`：`source` 代表門檻有外部來源，`team_default` 代表團隊預設值，報告時要說明。

## 6. 回饋與操作日誌（A 儲存，B 讀取）

見 `feedback_examples.json`、`op_log_examples.json`。標記規則在 `src/analytics/labels.py`：

- 同一 `event_id` 有多筆回覆時，以 `replied_at` 最晚的那筆為準（更正）
- `certainty = unsure` 或沒有 `replied_at` → `unknown`
- 沒有回覆時才查操作日誌，時間窗為 `[start_at − 15 分, decision_at + 5 分]`；有兩種以上設備 → `mixed`
- 找不到任何紀錄 → `unknown`，**不當作無操作**
