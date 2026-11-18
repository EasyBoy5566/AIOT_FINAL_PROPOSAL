from src.analytics.labels import link_labels
from src.knowledge.rules import recommend
from experiments.evaluate import compare_classifiers, metrics, time_split

INDOOR = {"type": "indoor", "status": "ok", "room_id": "r1", "measured_at": "2026-11-19T10:00:00+08:00",
          "latest": {"temperature_c": 29.0, "relative_humidity_pct": 72.0},
          "trend": {"window_minutes": 30, "delta_temperature_c": 0.2, "delta_relative_humidity_pct": 1.0}}
FC = {"type": "forecast", "status": "ok", "source": "CWA", "retrieved_at": "2026-11-19T07:00:00+08:00",
      "issued_at": None, "location": {"county": "臺中市", "district": "南區"},
      "target": {"start": "2026-11-19T09:00:00+08:00", "end": "2026-11-19T15:00:00+08:00"},
      "values": {"temperature_c": {"min": 24, "max": 26, "mean": 25}, "relative_humidity_pct": {"min": 62, "max": 75, "mean": 68},
                 "pop_pct": {"max": 60}, "weather": ["多雲短暫雨"]}}


def ids(r):
    return [x["rule_id"] for x in r["rules_fired"]]


def test_device_rules_and_device_state():
    r = recommend("device", indoor=INDOOR)
    assert r["status"] == "ok" and ids(r) == ["DK-01", "DK-03"]
    assert "不確定除濕機狀態" in r["rules_fired"][0]["suggestion"]
    assert all(e["time"] for e in r["evidence"])
    r2 = recommend("device", indoor=INDOOR, device_state={"dehumidifier": "on", "ac": "unknown"})
    assert "已確認開啟" in r2["rules_fired"][0]["suggestion"]
    r3 = recommend("device", indoor=INDOOR, preference={"comfort_temp_max": 30})
    assert ids(r3) == ["DK-01"]


def test_dk02_interpretation_only():
    ind = dict(INDOOR, latest={"temperature_c": 25, "relative_humidity_pct": 50},
               trend={"window_minutes": 30, "delta_temperature_c": 1.2, "delta_relative_humidity_pct": -5})
    r = recommend("device", indoor=ind)
    assert ids(r) == ["DK-02"]
    assert any("不能據此判定除濕機" in x for x in r["limitations"])


def test_device_missing_or_stale_indoor():
    assert recommend("device", indoor=None)["status"] == "insufficient_data"
    r = recommend("device", indoor=dict(INDOOR, status="stale", message="最後讀值 40 分鐘前"))
    assert r["status"] == "insufficient_data" and "過期" in r["missing"][0]


def test_outing_rain_and_clarify():
    r = recommend("outing", forecast=FC)
    assert ids(r) == ["DK-04"] and "帶" in r["rules_fired"][0]["suggestion"]
    assert any(e["ref"] == "臺中市南區" for e in r["evidence"])
    r = recommend("outing", forecast={"status": "missing", "message": "目標時段不完整或結束早於開始"})
    assert r["status"] == "needs_clarification"
    r = recommend("outing", forecast={"status": "error", "message": "API 連線失敗"})
    assert r["status"] == "insufficient_data"
    hot = dict(FC, values=dict(FC["values"], temperature_c={"min": 28, "max": 34, "mean": 31}, pop_pct={"max": 10}))
    assert ids(recommend("outing", forecast=hot)) == ["DK-06"]


def test_laundry_both_sources_and_partial():
    r = recommend("laundry", indoor=INDOOR, forecast=FC)
    assert r["status"] == "ok" and ids(r) == ["DK-04", "DK-05"]
    kinds = {e["kind"] for e in r["evidence"]}
    assert "indoor_relative_humidity" in kinds and "forecast_pop_max" in kinds
    p = recommend("laundry", indoor=INDOOR, forecast={"status": "error", "message": "x"})
    assert p["status"] == "partial" and ids(p) == ["DK-05"]
    assert recommend("laundry")["status"] == "insufficient_data"
    assert recommend("weather?")["status"] == "needs_clarification"


EV = [{"event_id": f"e{i}", "start_at": f"2026-11-19T{10+i:02d}:00:00+08:00",
       "decision_at": f"2026-11-19T{10+i:02d}:10:00+08:00"} for i in range(5)]


def test_label_linking_rules():
    fb = [{"event_id": "e0", "replied_at": "2026-11-19T10:20:00+08:00", "operation_label": "ac", "certainty": "sure"},
          {"event_id": "e0", "replied_at": "2026-11-19T10:30:00+08:00", "operation_label": "dehumidifier", "certainty": "sure"},  # 更正
          {"event_id": "e1", "replied_at": "2026-11-19T11:20:00+08:00", "operation_label": "ac", "certainty": "unsure"},
          {"event_id": "e4", "replied_at": None, "operation_label": None}]
    log = [{"operation_at": "2026-11-19T12:02:00+08:00", "operation_label": "ac", "certainty": "sure"},
           {"operation_at": "2026-11-19T12:05:00+08:00", "operation_label": "dehumidifier", "certainty": "sure"},
           {"operation_at": "2026-11-19T13:00:00+08:00", "operation_label": "none_confirmed", "certainty": "sure"}]
    lab = {e["event_id"]: (e["label"], e["label_source"]) for e in link_labels(EV, fb, log)}
    assert lab["e0"] == ("dehumidifier", "feedback")
    assert lab["e1"][0] == "unknown"
    assert lab["e2"] == ("mixed", "op_log")
    assert lab["e3"] == ("none_confirmed", "op_log")
    assert lab["e4"][0] == "unknown"           # 未回覆不當無操作


def test_time_split_no_overlap():
    ev = [{"start_at": f"2026-11-{d:02d}T10:00:00+08:00"} for d in range(1, 21)]
    tr, te, cut = time_split(ev, 0.3)
    assert len(te) == 6 and max(e["start_at"] for e in tr) < min(e["start_at"] for e in te) == cut


def test_metrics():
    m = metrics([1, 1, 0, 0], [1, 0, 0, 0], [0, 1])
    assert m["confusion_matrix"] == [[2, 0], [1, 1]]
    assert m["per_class"]["1"]["recall"] == 0.5 and m["per_class"]["1"]["precision"] == 1.0


def test_insufficient_samples_keeps_rule():
    feats = {"delta_temperature_c": -2, "delta_relative_humidity_pct": -3, "slope_temperature_c_per_min": -0.1,
             "slope_relative_humidity_pct_per_min": -0.1, "start_temperature_c": 26, "start_relative_humidity_pct": 65,
             "delta_dew_point_c": -1, "minutes_to_decision": 15, "hour_of_day": 10}
    ev = [dict(e, features=feats, label="ac") for e in EV] + [dict(EV[0], event_id="z", start_at="2026-11-19T20:00:00+08:00", features=feats, label="unknown")]
    r = compare_classifiers(ev, "binary")
    assert r["decision_tree"] is None and r["selected"] == "fixed_rule"
    assert r["n_excluded_unknown_or_other"] == 1 and "樣本不足" in r["limitations"][0]
