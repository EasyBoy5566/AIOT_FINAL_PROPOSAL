# DESIGN.md 更新草稿｜工作流 B

> 第 4 週和 A、C 一起合併進 DESIGN.md。標 `TODO` 的地方要等真實資料跑完再填。

## 4.1 補充：與 A 的資料格式對接（v1）

室內量測、操作日誌、候選事件與回覆的欄位以《室內感測與回饋資料格式 v1》（主責 高群軒）為準。
B 內部保留較明確的命名，差異集中在 `src/common/adapters.py`，不散落在各模組：

| 項目 | A v1 | B 內部 |
| --- | --- | --- |
| 時間 | UTC `YYYY-MM-DDTHH:mm:ss.SSSZ` | Asia/Taipei，秒精度 |
| 品質旗標 | `ok` / `missing` / `invalid_range` | 同左；不在值域者原名計數 |
| 明確無操作 | `none` | `none_confirmed` |
| 未知 | 欄位 `null` | `unknown` |
| 事件判定時間 | `detected_at` | `decision_at`（同值） |
| 事件版本 | `event_version`（整數，遞增） | `detector_version` / `config_version`（字串） |

量測去重以 A 的 `measurement_id` 為唯一鍵；歷史查詢為半開區間 `[from, to)`。
回覆取用：同一 `feedback_id` 取 `feedback_version` 最大者，同一事件優先取 `status = confirmed`。
只有 `confirmed` 提供標記，其餘為 unknown 並排除於分類之外。

## 4.1.1 共用門檻（thresholds/1.0.0）

依 A 規格〈共通規格〉，取樣頻率、過期時間與缺口門檻記在 `src/common/schema.py`，三方共用：

| 門檻 | 值 | 依據 |
| --- | --- | --- |
| 取樣頻率 | 每分鐘 1 筆 | 第一版，TODO：首次實測後確認 |
| 室內量測過期 | 10 分鐘 | 取樣頻率的 10 倍 |
| 時間缺口 | 5 分鐘 | 超過即切段 |
| 預報過期 | 6 小時 | 氣象署發布週期 |

評估報告的 manifest 記錄實際使用的門檻版本與數值。

## 4.2 補充：預報快照實際欄位

`forecast_snapshots(snapshot_id, location_id, retrieved_at, issued_at, valid_from, valid_to, forecast_payload, source, dataset_id, tool_version)`

- 產品：中央氣象署鄉鎮預報「未來 3 天逐 3 小時」，資料集代碼 F-D0047-001～085，每個縣市一組（對照表在 `cwa_forecast.COUNTY_DATASET_3DAY`）。
- `forecast_payload` 只存查詢的那個行政區，不存整個縣市。
- `issued_at`：來源沒有回傳就是 NULL。
- 新鮮度：6 小時（氣象署每 6 小時更新）。

## 5.2 趨勢與事件偵測（實作版本 events/1.0.0）

| 參數 | 值（cfg-2026w2-default） | 說明 |
| --- | --- | --- |
| 斷線切段 | > 5 分鐘 | 不同段之間不計算變化 |
| 平滑 | 3 點中位數 | 去掉單點跳動 |
| 斜率視窗 | 10 分鐘，最小平方法 | 斷線恢復後先累積一個視窗 |
| 斜率門檻 | 溫度 0.05 °C/分、濕度 0.25 %/分，任一個超過即可 | |
| 持續時間 | 從第一次超過門檻算起 ≥ 5 分鐘 | |
| 累積變化 | 溫度 ≥ 0.8 °C 或濕度 ≥ 3 % | |
| 事件合併 | 兩段間隔 ≤ 20 分鐘 | |
| 詢問間隔 | 30 分鐘（ask/1.0.0） | |

TODO：用訓練期實測資料確認門檻，記錄調整前後的事件數。

分類特徵（只用 `decision_at` 以前的資料）：溫度和濕度的變化量與斜率、起始溫濕度、露點變化、從開始到判斷的時間、時段。

## 5.3 規則基準與分類器（實作）

- 固定規則（fixed-rule/1.0.0）：溫度下降 ≥ 1.0 °C 或露點下降 ≥ 1.5 °C → 有操作。在看資料前訂定，不用測試集調整。
- 決策樹：`max_depth=3, min_samples_leaf=3, class_weight=balanced`。
- 切分：依事件開始時間，較晚 30% 作測試。
- 最低樣本：訓練 ≥ 20、測試 ≥ 10，每類訓練 ≥ 5、測試 ≥ 3；不到就不訓練決策樹，維持規則版。
- 標記：見 `labels.py`（labels/1.1.0）。unknown 與 other 排除在分類之外，並分別報告排除數。
  `other` 的語意由 A 規格允許但未定義，B 先訂為「無法判定、排除」，待 C 確認後再決定是否改為正類。
- 無操作日誌（A 的 `none`）須有觀察時段且涵蓋事件的 `[start_at, decision_at]` 才採用。

## 6. 規則表（rules/1.0.0）

| ID | 門檻 | 依據類型 |
| --- | --- | --- |
| DK-01 | 室內 RH ≥ 60% | 外部來源（EPA） |
| DK-02 | 溫度上升 > 0.5 °C 且 RH 下降 > 2%（近 30 分鐘） | 只做解讀，不判定設備 |
| DK-03 | 室內 ≥ 28 °C（可用偏好覆寫） | 團隊預設 |
| DK-04 | 降雨機率 ≥ 30%（≥ 70% 加強提醒） | 團隊預設 |
| DK-05 | 室內晾衣 RH ≥ 70% | 團隊預設 |
| DK-06 | 外出最高 ≥ 33 °C／最低 ≤ 15 °C | 團隊預設（新增，不是氣象署特報標準） |

## 9. 實驗結果

TODO：從 `experiments/results/<tag>/report.md` 帶入，並標明哪些是即時實測、哪些是資料回放。
