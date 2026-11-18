from datetime import timedelta

from src.analytics.ask_policy import AskConfig, replay_baseline, replay_merged_cooldown, should_ask
from src.analytics.events import EventConfig, detect_events, detect_runs
from src.analytics.quality import indoor_summary, valid_segments
from src.common.schema import fmt_time, parse_time

T0 = parse_time("2026-11-19T10:00")


def series(n, f, skip=()):
    """f(i) -> (T, RH)；skip 為缺掉的分鐘（模擬斷線）。"""
    out = []
    for i in range(n):
        if i in skip:
            continue
        t, h = f(i)
        out.append({"room_id": "r1", "measured_at": fmt_time(T0 + timedelta(minutes=i)),
                    "temperature_c": t, "relative_humidity_pct": h, "quality_flag": "ok"})
    return out


flat = lambda i: (26.0, 65.0)  # noqa: E731


def test_flat_no_event():
    assert detect_events(series(120, flat), "r1")["events"] == []


def test_single_spike_ignored():
    def f(i):
        return (32.0, 40.0) if i == 60 else (26.0, 65.0)
    assert detect_events(series(120, f), "r1")["events"] == []


def test_double_spike_ignored():
    def f(i):
        return (31.0, 45.0) if i in (60, 61) else (26.0, 65.0)
    assert detect_events(series(120, f), "r1")["events"] == []


def test_sustained_ac_like_drop_detected_with_decision_time_features():
    def f(i):
        if 30 <= i < 55:
            return 26.0 - 0.12 * (i - 30), 65.0 - 0.15 * (i - 30)
        if i >= 55:
            return 23.0, 61.25
        return 26.0, 65.0
    r = detect_events(series(120, f), "r1")
    assert len(r["events"]) == 1
    e = r["events"][0]
    dec = parse_time(e["decision_at"])
    assert T0 + timedelta(minutes=30) < dec <= T0 + timedelta(minutes=55)
    assert e["features"]["delta_temperature_c"] <= -0.8
    assert e["features"]["direction_temperature"] == "down"
    # 特徵只到 decision_at：決策時的變化量必小於事件結束後的總變化
    assert abs(e["features"]["delta_temperature_c"]) < abs(e["summary"]["total_delta_temperature_c"]) + 1e-9
    assert e["event_id"].startswith("ev-r1-")


def test_reconnect_jump_not_event():
    """斷線 20 分鐘，恢復後數值不同，但恢復後是平的 → 不產生事件。"""
    def f(i):
        return (26.0, 65.0) if i < 40 else (24.0, 58.0)
    r = detect_events(series(120, f, skip=range(40, 60)), "r1")
    assert r["events"] == []
    assert r["quality"]["gaps"] and r["quality"]["gaps"][0]["minutes"] == 21.0


def test_invalid_values_filtered():
    rows = series(30, flat)
    rows[5]["temperature_c"] = None
    rows[6]["relative_humidity_pct"] = 150
    rows[7]["quality_flag"] = "sensor_error"      # 不在 A v1 的值域：原樣計數，不靜默改名
    rows.append(dict(rows[0]))
    segs, rep = valid_segments(rows)
    assert rep.valid == 27 and rep.duplicates == 1
    # 原因名與 A 規格 §1 的 quality_flag 同一組值
    assert rep.invalid == {"missing": 1, "invalid_range": 1, "sensor_error": 1}


def two_bumps(gap_min):
    b2 = 30 + 20 + gap_min

    def f(i):
        base_t = 26.0
        if 30 <= i < 50:
            return base_t - 0.1 * (i - 30), 65.0
        if 50 <= i < b2:
            return base_t - 2.0, 65.0
        if b2 <= i < b2 + 20:
            return base_t - 2.0 - 0.1 * (i - b2), 65.0
        return base_t - 4.0, 65.0
    return series(b2 + 60, f)


def test_merge_close_runs_and_ask_policies():
    rows = two_bumps(8)
    runs, _ = detect_runs(rows, "r1")
    assert len(runs) == 2
    events = detect_events(rows, "r1")["events"]
    assert len(events) == 1 and events[0]["n_runs"] == 2
    base = replay_baseline(runs)
    merged, sup = replay_merged_cooldown(runs)
    assert len(base) == 2 and len(merged) == 1


def test_far_runs_separate_and_cooldown():
    rows = two_bumps(40)  # 平台 40 分；扣掉斜率視窗前後延伸後仍 > merge_gap 20 分 → 兩個事件
    events = detect_events(rows, "r1")["events"]
    assert len(events) == 2
    runs, _ = detect_runs(rows, "r1")
    merged, sup = replay_merged_cooldown(runs, AskConfig(cooldown_min=90))
    assert len(merged) == 1 and len(sup) == 1
    merged2, sup2 = replay_merged_cooldown(runs, AskConfig(cooldown_min=10))
    assert len(merged2) == 2 and not sup2


def test_should_ask_online():
    e = {"event_id": "ev-x", "decision_at": "2026-11-19T10:20:00+08:00"}
    assert should_ask(e, None)["ask"] is True
    d = should_ask(e, "2026-11-19T10:00:00+08:00")
    assert d["ask"] is False and d["ask_status"] == "suppressed_cooldown"
    assert should_ask(e, "2026-11-19T09:40:00+08:00")["ask"] is True


def test_indoor_summary_fresh_and_stale():
    rows = series(40, lambda i: (26 + 0.02 * i, 65.0))
    now = T0 + timedelta(minutes=41)
    s = indoor_summary(rows, now)
    assert s["status"] == "ok" and s["trend"]["delta_temperature_c"] > 0
    assert indoor_summary(rows, now + timedelta(hours=1))["status"] == "stale"
    assert indoor_summary([], now)["status"] == "missing"


def test_config_versioned():
    assert detect_events(series(10, flat), "r1")["config"]["version"] == EventConfig().version
