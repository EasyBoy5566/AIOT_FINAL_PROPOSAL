"""對接《室內感測與回饋資料格式 v1》（主責 高群軒）的互通測試。

每一條對應一處 A 規格與 B 實作原本不相容的地方；B 的內部值與 A 的線上值
在 src/common/adapters.py 轉換，這裡驗證轉換後的行為。
"""
import sqlite3
from datetime import timedelta

import pytest

from src.analytics.events import EventConfig, _gap_adjacent, detect_events
from src.analytics.labels import label_counts, latest_feedback, link_labels
from src.analytics.quality import load_measurements_sqlite, valid_segments
from src.common.adapters import event_to_a, feedback_from_a, op_log_from_a
from src.common.schema import (
    FEATURES_VERSION,
    fmt_utc_ms,
    normalize_operation_label,
    parse_time,
    to_a_operation_label,
    to_binary,
)

ROOM = "room-01"
BASE = parse_time("2026-11-18T10:00:00+08:00")      # = 2026-11-18T02:00:00.000Z


def a_measurement(i, t=26.0, rh=65.0, flag="ok", mid=None):
    """A §1 的量測紀錄（接收端保存後的形態），時間為 A 的 UTC 格式。"""
    at = fmt_utc_ms(BASE + timedelta(minutes=i))
    return {"measurement_id": mid or "sensor-01:boot-a27:%d" % i, "room_id": ROOM, "device_id": "sensor-01",
            "measured_at": at, "received_at": at,
            "temperature_c": t, "relative_humidity_pct": rh, "quality_flag": flag}


# --- 1 時間格式 -------------------------------------------------------------

def test_a_utc_time_parses_and_roundtrips():
    """A 以 UTC `...SSSZ` 儲存，B 內部用 +08:00；兩個方向都要對得上。"""
    assert parse_time("2026-11-18T10:00:00.000Z").isoformat() == "2026-11-18T18:00:00+08:00"
    assert fmt_utc_ms("2026-11-18T18:00:00+08:00") == "2026-11-18T10:00:00.000Z"
    assert fmt_utc_ms(parse_time("2026-11-18T10:00:00.000Z")) == "2026-11-18T10:00:00.000Z"


def test_sqlite_range_matches_a_storage_format():
    """A 的 measured_at 以 UTC 字串存。查詢邊界若用 +08:00 字串比較會整段取錯。"""
    conn = sqlite3.connect(":memory:")
    conn.execute("CREATE TABLE measurements (measurement_id TEXT, room_id TEXT, device_id TEXT, "
                 "measured_at TEXT, received_at TEXT, temperature_c REAL, relative_humidity_pct REAL, "
                 "quality_flag TEXT)")
    rows = [a_measurement(i) for i in range(0, 10)]
    conn.executemany("INSERT INTO measurements VALUES (:measurement_id,:room_id,:device_id,:measured_at,"
                     ":received_at,:temperature_c,:relative_humidity_pct,:quality_flag)", rows)
    # 半開區間 [10:02, 10:05) 台灣時間 = UTC 02:02–02:05，應取到 02:02、02:03、02:04
    got = load_measurements_sqlite(conn, ROOM, "2026-11-18T10:02:00+08:00", "2026-11-18T10:05:00+08:00")
    assert [r["measured_at"] for r in got] == ["2026-11-18T02:02:00.000Z", "2026-11-18T02:03:00.000Z",
                                               "2026-11-18T02:04:00.000Z"]


# --- 2 品質旗標 -------------------------------------------------------------

def test_a_quality_flags_excluded_under_a_names():
    rows = [a_measurement(0), a_measurement(1, flag="missing"), a_measurement(2, flag="invalid_range")]
    segs, rep = valid_segments(rows)
    assert rep.valid == 1
    assert rep.invalid == {"missing": 1, "invalid_range": 1}


def test_a_measurement_id_dedupes_resend():
    """A §1：measurement_id 為唯一鍵，重送同一筆只保留一筆。"""
    rows = [a_measurement(0), a_measurement(1), dict(a_measurement(1))]
    segs, rep = valid_segments(rows)
    assert rep.valid == 2 and rep.duplicates == 1


# --- 3 操作標記 -------------------------------------------------------------

def test_a_none_maps_to_none_confirmed_not_unknown():
    """A 用 `none` 表示明確無操作。若當成未知，分類就少掉所有負類樣本。"""
    assert normalize_operation_label("none") == "none_confirmed"
    assert to_binary("none") == 0
    assert to_a_operation_label("none_confirmed") == "none"


def test_a_other_is_kept_but_excluded_from_binary():
    """other 是「有回覆但不屬於三種操作」，無法判定有無操作，排除但單獨計數。"""
    assert normalize_operation_label("other") == "other"
    assert to_binary("other") is None
    assert to_a_operation_label("unknown") is None


def test_unrecognised_label_raises():
    """封閉集合；拼錯要當場暴露，不可靜默變成 unknown。"""
    with pytest.raises(ValueError):
        normalize_operation_label("aircon")


# --- 4 回覆狀態與版本 -------------------------------------------------------

def a_feedback(fid, eid, status, label, replied="2026-11-18T02:09:00.000Z", version=1,
               asked="2026-11-18T02:08:20.000Z", expires="2026-11-18T02:23:20.000Z"):
    return {"feedback_id": fid, "event_id": eid, "asked_at": asked, "expires_at": expires,
            "replied_at": replied, "operation_at": None, "operation_label": label,
            "status": status, "feedback_version": version, "updated_at": replied}


@pytest.mark.parametrize("status,label,expected", [
    ("confirmed", "ac", "ac"),
    ("confirmed", "none", "none_confirmed"),
    ("confirmed", "other", "other"),
    ("uncertain", None, "unknown"),
    ("pending", None, "unknown"),
    ("expired", None, "unknown"),
])
def test_only_confirmed_carries_a_label(status, label, expected):
    """A §4「評估用途」欄：只有 confirmed 能納入標記候選。"""
    replied = "2026-11-18T02:09:00.000Z" if status in ("confirmed", "uncertain") else None
    f = feedback_from_a(a_feedback("fb-1", "e0", status, label, replied=replied))
    assert f["operation_label"] == expected


def test_feedback_version_correction_wins():
    """同一 feedback_id 的更正：取 feedback_version 最大者。"""
    rows = [a_feedback("fb-1", "e0", "confirmed", "ac", version=1),
            a_feedback("fb-1", "e0", "confirmed", "dehumidifier", version=2)]
    assert latest_feedback(rows)["e0"]["operation_label"] == "dehumidifier"
    assert latest_feedback(list(reversed(rows)))["e0"]["operation_label"] == "dehumidifier"


def test_multiple_asks_prefers_confirmed():
    """同一事件問兩次：先逾時、後確認，應採用確認的那次。"""
    rows = [a_feedback("fb-1", "e0", "expired", None, replied=None),
            a_feedback("fb-2", "e0", "confirmed", "window", replied="2026-11-18T03:00:00.000Z")]
    best = latest_feedback(rows)["e0"]
    assert best["operation_label"] == "window" and best["feedback_id"] == "fb-2"


def test_late_reply_flagged():
    """A §4：replied_at > expires_at 為晚到回覆，要能追溯。"""
    f = feedback_from_a(a_feedback("fb-1", "e0", "confirmed", "ac", replied="2026-11-18T03:00:00.000Z"))
    assert f["is_late"] is True
    assert feedback_from_a(a_feedback("fb-2", "e1", "confirmed", "ac"))["is_late"] is False


# --- 5 操作日誌 -------------------------------------------------------------

EVENT = {"event_id": "ev-1", "room_id": ROOM, "start_at": "2026-11-18T10:02:00+08:00",
         "decision_at": "2026-11-18T10:08:00+08:00", "end_at": "2026-11-18T10:30:00+08:00"}


def a_op_log(label, at, end=None, action=None, room=ROOM):
    return {"operation_id": "op-1", "room_id": room, "operation_at": at, "observation_end_at": end,
            "operation_label": label, "action": action, "recorded_at": at}


def test_op_log_none_needs_observation_window_covering_event():
    """A §2 的無操作是一個觀察時段；只有涵蓋整個事件才算明確無操作。"""
    covering = a_op_log("none", "2026-11-18T01:30:00.000Z", end="2026-11-18T03:00:00.000Z")
    partial = a_op_log("none", "2026-11-18T02:05:00.000Z", end="2026-11-18T03:00:00.000Z")
    assert link_labels([EVENT], op_log=[op_log_from_a(covering)])[0]["label"] == "none_confirmed"
    assert link_labels([EVENT], op_log=[op_log_from_a(partial)])[0]["label"] == "unknown"


def test_op_log_device_operation_point_match():
    row = op_log_from_a(a_op_log("ac", "2026-11-18T02:01:00.000Z", action="on"))
    assert link_labels([EVENT], op_log=[row])[0]["label"] == "ac"


def test_op_log_other_room_ignored():
    row = op_log_from_a(a_op_log("ac", "2026-11-18T02:01:00.000Z", action="on", room="room-02"))
    assert link_labels([EVENT], op_log=[row])[0]["label"] == "unknown"


def test_label_counts_reports_other_separately():
    events = [dict(EVENT, event_id="ev-%d" % i) for i in range(3)]
    fb = [a_feedback("fb-0", "ev-0", "confirmed", "ac"),
          a_feedback("fb-1", "ev-1", "confirmed", "other"),
          a_feedback("fb-2", "ev-2", "expired", None, replied=None)]
    got = label_counts(link_labels(events, feedback=fb))
    assert got == {"ac": 1, "other": 1, "unknown": 1}


# --- 6 事件寫回 A -----------------------------------------------------------

def test_event_to_a_shape():
    """A §3：detected_at、整數 event_version、持續中的事件 end_at 為 null。"""
    ev = {"event_id": "ev-1", "room_id": ROOM, "start_at": "2026-11-18T10:02:00+08:00",
          "decision_at": "2026-11-18T10:08:00+08:00", "end_at": "2026-11-18T10:30:00+08:00",
          "detected_at": "2026-11-18T10:08:00+08:00",
          "features": {"delta_temperature_c": -1.3}, "summary": {}, "quality_flag": "ok"}
    rec = event_to_a(ev)
    assert rec["detected_at"] == "2026-11-18T02:08:00.000Z"
    assert rec["end_at"] == "2026-11-18T02:30:00.000Z"
    assert rec["event_version"] == 1 and isinstance(rec["event_version"], int)
    assert rec["features"]["_version"] == FEATURES_VERSION
    assert rec["quality_flag"] in ("ok", "gap")

    ongoing = event_to_a(dict(ev, summary={"open_at_segment_end": True}), event_version=2)
    assert ongoing["end_at"] is None and ongoing["event_version"] == 2


def test_event_version_is_not_the_detector_version():
    """event_version 是 A 的整數版本號；B 的偵測器與門檻版本另外帶。"""
    rows = [a_measurement(i, t=26.0 - 0.06 * i, rh=65.0 - 0.3 * i) for i in range(40)]
    res = detect_events(rows, ROOM)
    assert res["events"], "測試資料應產生至少一個事件"
    e = res["events"][0]
    assert e["event_version"] == 1
    assert e["detector_version"] == res["detector_version"]
    assert e["config_version"] == EventConfig().version
    assert e["detected_at"] == e["decision_at"]


def test_gap_adjacent_flag():
    """緊接在斷線恢復之後的事件標為 gap（A §3）。"""
    cfg = EventConfig()
    start = parse_time("2026-11-18T10:30:00+08:00")
    gaps = [{"from": "2026-11-18T10:00:00+08:00", "to": "2026-11-18T10:20:00+08:00", "minutes": 20.0}]
    assert _gap_adjacent(start, gaps, cfg) is True          # 10 分鐘 <= 10 + 5
    assert _gap_adjacent(start + timedelta(minutes=30), gaps, cfg) is False
    assert _gap_adjacent(start, [], cfg) is False
