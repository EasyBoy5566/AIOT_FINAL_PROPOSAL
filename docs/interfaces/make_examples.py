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
from src.common.schema import parse_time, fmt_time, fmt_utc_ms
from src.common.adapters import event_to_a

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
ROOM = "room-01"
def rows(n, f):
    """A §1 的量測紀錄（接收端保存後的形態），時間為 A 的 UTC 格式。"""
    out = []
    for i in range(n):
        t = T0 + timedelta(minutes=i)
        out.append({"measurement_id": "sensor-01:boot-a27:%d" % i, "room_id": ROOM, "device_id": "sensor-01",
                    "measured_at": fmt_utc_ms(t), "received_at": fmt_utc_ms(t + timedelta(seconds=1)),
                    "temperature_c": f(i)[0], "relative_humidity_pct": f(i)[1], "quality_flag": "ok"})
    return out
dump("measurements_input_from_A.json", {"rows": rows(3, lambda i: (26.0, 65.0))})
ac = lambda i: ((26.0 - 0.12 * (i - 30), 65.0 - 0.15 * (i - 30)) if 30 <= i < 55 else ((23.0, 61.25) if i >= 55 else (26.0, 65.0)))
det = detect_events(rows(90, ac), ROOM)
dump("event_detected.json", det)
dump("event_record_to_A.json", {"_note": "B 的事件寫回 A §3 的保存格式（src.common.adapters.event_to_a）",
                               "rows": [event_to_a(e) for e in det["events"]]})
dump("event_none_spike.json", detect_events(rows(90, lambda i: (32.0, 40.0) if i == 50 else (26.0, 65.0)), ROOM))
ev = det["events"][0]
dump("ask_decision.json", {"first": should_ask(ev, None), "within_cooldown": should_ask(ev, ev["decision_at"])})
ind = indoor_summary(rows(60, lambda i: (29.0, 70.0 + 0.05 * i)), T0 + timedelta(minutes=60))
dump("indoor_summary.json", ind)
dump("recommend_device.json", recommend("device", indoor=ind, device_state={"ac": "unknown", "dehumidifier": "off", "confirmed_at": None}))
dump("recommend_outing.json", recommend("outing", forecast=fc))
dump("recommend_laundry.json", recommend("laundry", indoor=ind, forecast=fc))
dump("recommend_laundry_partial.json", recommend("laundry", indoor=ind, forecast=cf.get_forecast("臺中市", "南區", "2026-11-19T09:00", "2026-11-19T15:00", api_key="k", now=NOW, fetcher=fail)))
dump("recommend_outing_clarify.json", recommend("outing", forecast=cf.get_forecast("臺中市", "南區", "2026-11-19T15:00", "2026-11-19T09:00", api_key="k", now=NOW, fetcher=ok)))
A = fmt_utc_ms
dump("feedback_examples.json", {"_note": "欄位依 A 規格 §4；B 取用順序見 src/analytics/labels.py", "rows": [
  {"feedback_id": "fb-001", "event_id": ev["event_id"], "asked_at": A(ev["decision_at"]),
   "expires_at": A("2026-11-19T10:45:00+08:00"), "replied_at": A("2026-11-19T10:50:00+08:00"),
   "operation_at": A("2026-11-19T10:28:00+08:00"), "operation_label": "ac", "status": "confirmed",
   "feedback_version": 1, "updated_at": A("2026-11-19T10:50:00+08:00"),
   "_note": "晚到回覆：replied_at > expires_at"},
  {"feedback_id": "fb-001", "event_id": ev["event_id"], "asked_at": A(ev["decision_at"]),
   "expires_at": A("2026-11-19T10:45:00+08:00"), "replied_at": A("2026-11-19T11:05:00+08:00"),
   "operation_at": A("2026-11-19T10:28:00+08:00"), "operation_label": "mixed", "status": "confirmed",
   "feedback_version": 2, "updated_at": A("2026-11-19T11:05:00+08:00"),
   "_note": "更正：同一 feedback_id 取 feedback_version 最大者 → mixed"},
  {"feedback_id": "fb-002", "event_id": ev["event_id"], "asked_at": A(ev["decision_at"]),
   "expires_at": A("2026-11-19T10:45:00+08:00"), "replied_at": A("2026-11-19T10:40:00+08:00"),
   "operation_at": None, "operation_label": None, "status": "uncertain",
   "feedback_version": 1, "updated_at": A("2026-11-19T10:40:00+08:00"),
   "_note": "有回覆但不確定 → unknown，不會蓋掉同事件的 confirmed"},
  {"feedback_id": "fb-003", "event_id": "ev-room-01-20261119T1400",
   "asked_at": A("2026-11-19T14:15:00+08:00"), "expires_at": A("2026-11-19T14:30:00+08:00"),
   "replied_at": None, "operation_at": None, "operation_label": None, "status": "expired",
   "feedback_version": 1, "updated_at": A("2026-11-19T14:30:00+08:00"),
   "_note": "逾時未回覆 → unknown，不當作無操作"}]})
dump("op_log_examples.json", {"_note": "欄位依 A 規格 §2；無操作須有 observation_end_at 且涵蓋事件", "rows": [
  {"operation_id": "op-001", "room_id": ROOM, "operation_at": A("2026-11-19T10:28:00+08:00"),
   "observation_end_at": None, "operation_label": "ac", "action": "on",
   "recorded_at": A("2026-11-19T10:28:30+08:00"), "_note": "設備操作：單一時間點"},
  {"operation_id": "op-002", "room_id": ROOM, "operation_at": A("2026-11-19T13:00:00+08:00"),
   "observation_end_at": A("2026-11-19T15:00:00+08:00"), "operation_label": "none", "action": None,
   "recorded_at": A("2026-11-19T15:01:00+08:00"),
   "_note": "無操作觀察時段 13:00–15:00；只有涵蓋整個事件才算 none_confirmed"},
  {"operation_id": "op-003", "room_id": "room-02", "operation_at": A("2026-11-19T18:10:00+08:00"),
   "observation_end_at": None, "operation_label": "window", "action": "open",
   "recorded_at": A("2026-11-19T18:11:00+08:00"), "_note": "其他房間 → 不採用"}]})
print("ok", len(list(OUT.glob("*.json"))))
