# 工作流 B｜氣象、分析與評估（鍾易憲）

## 安裝與測試

```bash
python -m pip install -r requirements.txt     # 只有評估的決策樹需要 scikit-learn；其他用標準函式庫
python -m pytest -q                           # 28 項測試
export CWA_API_KEY=CWA-xxxxxxxx               # 到 opendata.cwa.gov.tw 會員中心取得授權碼
```

## 檔案

| 檔案 | 內容 | 週次 |
| --- | --- | --- |
| `src/common/schema.py` | 共用時間、單位、狀態、操作標記 | W1 |
| `src/acquisition/cwa_forecast.py` | 氣象署鄉鎮預報工具、快照保存與回放 | W1 |
| `src/analytics/quality.py` | 量測品質檢查、有效視窗、室內現況摘要 | W2 |
| `src/analytics/events.py` | 持續變化判定、事件合併 | W2 |
| `src/analytics/ask_policy.py` | 詢問間隔、兩種詢問政策回放 | W2–W3 |
| `src/knowledge/rules.py` | DK-01～DK-06 設備／外出／曬衣規則 | W2 |
| `src/analytics/labels.py` | 回覆與操作日誌 → 事件標記 | W2–W3 |
| `experiments/evaluate.py` | 時間切分、固定規則 vs 決策樹、詢問負擔 | W3 |
| `experiments/run_all.py` | 一鍵評估並產生報告 | W3–W4 |
| `experiments/simulate.py` | 合成資料，只用來測流程 | — |
| `docs/interfaces/` | 介面說明與成功／失敗範例 | W1–W2 |
| `docs/DESIGN_B_section_draft.md` | DESIGN.md 的 B 段更新草稿 | W4 |

## 計畫逐項對照

✅ = 程式已完成並有測試　🔲 = 需要你用真實資料或和組員一起完成

### 第 1 週

- ✅ 預報產品：鄉鎮預報「未來 3 天逐 3 小時」（F-D0047-001～085，各縣市一組），依行政區、目標時段取值
- 🔲 **交付一筆真實預報快照**：
  ```bash
  python -m src.acquisition.cwa_forecast --county 臺中市 --district 南區 \
      --start 2026-11-19T09:00 --end 2026-11-19T15:00 --save data/forecast_snapshots --db data/aiot.db
  ```
  第一次跑時請核對回傳的縣市是否正確（程式會自動檢查，對不上會回 `error`）。把產生的快照複製一份到 `docs/interfaces/examples/forecast_real_snapshot.json`。
- ✅ 缺資料／API 失敗／快照過期的回傳方式（見 `docs/interfaces/B_interfaces.md` §1）
- 🔲 和 A、C 確認 `schema.py` 的共用欄位

### 第 2 週

- ✅ 以有效視窗判定持續變化、合併事件、詢問間隔
- ✅ 設備、外出、曬衣規則，回傳建議候選、規則編號、證據、限制
- ✅ 短暫跳點、斷線重連、持續變化測試（`tests/test_events.py`）
- ✅ 標記方式草案（`labels.py`）；🔲 和 A、C 確認後定版
- 🔲 用真實量測確認門檻：先看前一週資料的事件數。太多就提高 `EventConfig` 的門檻，太少就降低。**改門檻只看訓練期資料**，改完把 `version` 改名

### 第 3 週

- ✅ 完整事件分組、時間切分、只用 `decision_at` 前的特徵
- ✅ 固定規則 vs 決策樹（`max_depth=3`），precision、recall、macro-F1、混淆矩陣、類別分布
- ✅ 同一批事件回放兩種詢問政策：詢問量、每小時詢問、重複詢問率、實際回覆率
- ✅ 樣本不足時不訓練決策樹，記錄原因並維持規則版
- 🔲 用 A 的匯出跑一次：
  ```bash
  python -m experiments.run_all --measurements export/measurements.json \
      --feedback export/feedback.json --op-log export/op_log.json --room room1 --tag real-w3
  ```
  結果在 `experiments/results/real-w3/`，`manifest.json` 有資料指紋與所有版本

### 第 4 週

- ✅ 報告產生器（`report.md`，會依資料來源標示實測或合成）
- ✅ DESIGN.md B 段草稿
- 🔲 補失敗案例分析、填入實際結果、完成期末簡報的實驗與結論段落

## 先講清楚的限制

- 固定規則和偵測門檻是根據常識訂的初始值，還沒用真實資料驗證。
- `experiments/results/dryrun-synthetic/` 是合成資料，只證明流程能跑，**不能寫進報告或簡報**。
- 規則門檻有兩種：EPA 60% 濕度有外部來源；其他（28°C、降雨機率 30%／70%、33°C／15°C、晾衣 70%）是團隊預設，報告要寫明。
