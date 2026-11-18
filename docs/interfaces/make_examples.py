"""重新產生 docs/interfaces/examples/ 的範例（改欄位後執行一次）。
python -m docs.interfaces.make_examples
預報範例使用格式範例 fixture（假資料），真實快照請以 CLI 取得後另存 forecast_real_snapshot.json。
"""
import json, urllib.error
from datetime import timedelta
from pathlib import Path
from src.acquisition import cwa_forecast as cf
from src.analytics.events import detect_events
from src.analytics.ask_policy import should_ask
from src.analytics.quality import indoor_summary
from src.knowledge.rules import recommend
from src.common.schema import parse_time, fmt_time

OUT = Path(__file__).parent / "examples"
FIX = Path("tests/fixtures/cwa_F-D0047-073_format_sample.json").read_bytes()
NOW = parse_time("2026-11-19T07:00")
ok = lambda u, t: FIX
def fail(u, t): raise urllib.error.URLError("timed out")
def dump(name, obj):
    obj = {"_example": "由 docs/interfaces/make_examples.py 產生；預報數值來自格式範例假資料", **obj} if isinstance(obj, dict) else obj
    (OUT / name).write_text(json.dumps(obj, ensure_ascii=False, indent=2), encoding="utf-8")

OUT.mkdir(parents=True, exist_ok=True)
fc = cf.get_forecast("臺中市", "南區", "2026-11-19T09:00", "2026-11-19T15:00", api_key="k", now=NOW, fetcher=ok)
dump("forecast_ok.json", fc)
dump("forecast_error_api.json", cf.get_forecast("臺中市", "南區", "2026-11-19T09:00", "2026-11-19T15:00", api_key="k", now=NOW, fetcher=fail))
dump("forecast_missing_time.json", cf.get_forecast("臺中市", "南區", "2026-11-25T09:00", "2026-11-25T12:00", api_key="k", now=NOW, fetcher=ok))
dump("forecast_stale.json", cf.check_freshness(fc, NOW + timedelta(hours=7)))

T0 = parse_time("2026-11-19T10:00")
def rows(n, f):
    return [{"room_id": "room1", "measured_at": fmt_time(T0 + timedelta(minutes=i)), "temperature_c": f(i)[0],
             "relative_humidity_pct": f(i)[1], "quality_flag": "ok"} for i in range(n)]
dump("measurements_input_from_A.json", {"rows": rows(3, lambda i: (26.0, 65.0))})
ac = lambda i: ((26.0 - 0.12 * (i - 30), 65.0 - 0.15 * (i - 30)) if 30 <= i < 55 else ((23.0, 61.25) if i >= 55 else (26.0, 65.0)))
det = detect_events(rows(90, ac), "room1")
dump("event_detected.json", det)
dump("event_none_spike.json", detect_events(rows(90, lambda i: (32.0, 40.0) if i == 50 else (26.0, 65.0)), "room1"))
ev = det["events"][0]
dump("ask_decision.json", {"first": should_ask(ev, None), "within_cooldown": should_ask(ev, ev["decision_at"])})
ind = indoor_summary(rows(60, lambda i: (29.0, 70.0 + 0.05 * i)), T0 + timedelta(minutes=60))
dump("indoor_summary.json", ind)
dump("recommend_device.json", recommend("device", indoor=ind, device_state={"ac": "unknown", "dehumidifier": "off", "confirmed_at": None}))
dump("recommend_outing.json", recommend("outing", forecast=fc))
dump("recommend_laundry.json", recommend("laundry", indoor=ind, forecast=fc))
dump("recommend_laundry_partial.json", recommend("laundry", indoor=ind, forecast=cf.get_forecast("臺中市", "南區", "2026-11-19T09:00", "2026-11-19T15:00", api_key="k", now=NOW, fetcher=fail)))
dump("recommend_outing_clarify.json", recommend("outing", forecast=cf.get_forecast("臺中市", "南區", "2026-11-19T15:00", "2026-11-19T09:00", api_key="k", now=NOW, fetcher=ok)))
dump("feedback_examples.json", {"rows": [
  {"event_id": ev["event_id"], "asked_at": ev["decision_at"], "replied_at": "2026-11-19T10:50:00+08:00", "operation_at": "2026-11-19T10:28:00+08:00", "operation_label": "ac", "certainty": "sure"},
  {"event_id": ev["event_id"], "asked_at": ev["decision_at"], "replied_at": "2026-11-19T11:05:00+08:00", "operation_at": "2026-11-19T10:28:00+08:00", "operation_label": "mixed", "certainty": "sure", "_note": "更正：以 replied_at 最晚者為準"},
  {"event_id": "ev-room1-20261119T1400", "asked_at": "2026-11-19T14:15:00+08:00", "replied_at": None, "operation_at": None, "operation_label": None, "certainty": None, "_note": "未回覆 → unknown"}]})
dump("op_log_examples.json", {"rows": [
  {"operation_at": "2026-11-19T10:28:00+08:00", "operation_label": "ac", "certainty": "sure", "note": "開冷氣 26 度"},
  {"operation_at": "2026-11-19T13:00:00+08:00", "operation_label": "none_confirmed", "certainty": "sure", "note": "13:00-15:00 確認無操作"},
  {"operation_at": "2026-11-19T18:10:00+08:00", "operation_label": "window", "certainty": "unsure", "note": "不確定幾點開窗 → 不採用"}]})
print("ok", len(list(OUT.glob("*.json"))))
