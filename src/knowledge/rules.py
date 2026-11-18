"""設備、外出、曬衣的領域規則（DESIGN.md §6 DK-01～DK-05，另加 DK-06 外出溫度提醒）。

負責人：鍾易憲（工作流 B）

給 C 的入口：
    recommend(scenario, indoor=None, forecast=None, device_state=None, preference=None) -> dict
回傳「建議候選」：規則編號、證據（值／時間／來源）、限制、狀態。C 的模板或代理人只能根據這份
候選產生文字，不得自行補數值。

門檻分兩類，在 RULES 表中以 basis 標明：
- source：有外部來源（EPA 60% RH）
- team_default：團隊預設值，可由使用者偏好覆寫，報告時須說明不是官方標準
"""
from __future__ import annotations

from typing import Any

from src.common.schema import Status

RULESET_VERSION = "rules/1.0.0"

RULES: dict[str, dict] = {
    "DK-01": {"title": "室內濕度持續偏高", "threshold": {"relative_humidity_pct": 60},
              "basis": "source", "source_ref": "[7] US EPA, A Brief Guide to Mold, Moisture and Your Home",
              "limitation": "60% 為室內濕度參考值，不是舒適度或黴菌問題的診斷。"},
    "DK-02": {"title": "溫度上升且濕度下降的解讀", "threshold": {},
              "basis": "source", "source_ref": "[8] NOAA/NWS, Dew Point vs. Humidity",
              "limitation": "溫度上升本身就會讓相對濕度下降；不能據此判定除濕機已開啟。"},
    "DK-03": {"title": "室內偏熱", "threshold": {"temperature_c": 28},
              "basis": "team_default", "source_ref": "[4][6] 熱舒適受多項環境與個人因素影響",
              "limitation": "未量測風速、輻射、衣著與活動量；28°C 為團隊預設，可依個人偏好調整。"},
    "DK-04": {"title": "目的地／時段可能降雨", "threshold": {"pop_pct": 30, "pop_pct_high": 70},
              "basis": "team_default", "source_ref": "[9][10] 中央氣象署鄉鎮預報 3 小時降雨機率",
              "limitation": "降雨機率是預報，不等於實際會下雨；鄉鎮預報代表點為區公所，與實際位置可能不同。"},
    "DK-05": {"title": "室內晾衣條件", "threshold": {"relative_humidity_pct": 70},
              "basis": "team_default", "source_ref": "[7] 室內濕度參考；晾衣門檻為團隊預設",
              "limitation": "不預測乾燥時間；衣物材質、厚度、通風會影響結果。"},
    "DK-06": {"title": "外出氣溫提醒", "threshold": {"temperature_c_hot": 33, "temperature_c_cold": 15},
              "basis": "team_default", "source_ref": "[9] 中央氣象署鄉鎮預報溫度",
              "limitation": "33°C／15°C 為團隊預設提醒門檻，不是氣象署的高溫或低溫特報標準。"},
}

SCENARIOS = ("device", "outing", "laundry")


def _ev(kind: str, value: Any, unit: str | None, time: Any, source: str, ref: str | None = None) -> dict:
    return {"kind": kind, "value": value, "unit": unit, "time": time, "source": source, "ref": ref}


def _fire(rule_id: str, suggestion: str, evidence: list[dict], extra_limits: list[str] | None = None) -> dict:
    r = RULES[rule_id]
    return {"rule_id": rule_id, "rule_version": RULESET_VERSION, "title": r["title"], "basis": r["basis"],
            "source_ref": r["source_ref"], "suggestion": suggestion, "evidence": evidence,
            "limitations": [r["limitation"]] + (extra_limits or [])}


def _indoor_evidence(indoor: dict) -> list[dict]:
    src = f"室內感測（{indoor.get('room_id', '房間')}）"
    t = indoor.get("measured_at")
    ev = [_ev("indoor_temperature", indoor["latest"]["temperature_c"], "°C", t, src),
          _ev("indoor_relative_humidity", indoor["latest"]["relative_humidity_pct"], "%", t, src)]
    if indoor.get("trend"):
        tr = indoor["trend"]
        ev.append(_ev("indoor_trend", {"delta_temperature_c": tr["delta_temperature_c"],
                                       "delta_relative_humidity_pct": tr["delta_relative_humidity_pct"]},
                      None, f"近 {tr['window_minutes']} 分鐘", src))
    return ev


def _forecast_evidence(fc: dict) -> list[dict]:
    loc = f"{fc['location']['county']}{fc['location']['district']}"
    when = f"{fc['target']['start']}～{fc['target']['end']}"
    src = f"{fc['source']}，取得於 {fc['retrieved_at']}" + (f"，發布於 {fc['issued_at']}" if fc.get("issued_at") else "")
    v = fc["values"] or {}
    out = []
    for key, kind, unit in (("temperature_c", "forecast_temperature", "°C"),
                            ("relative_humidity_pct", "forecast_relative_humidity", "%"),
                            ("pop_pct", "forecast_pop_max", "%")):
        if v.get(key) is not None:
            out.append(_ev(kind, v[key], unit, when, src, loc))
    if v.get("weather"):
        out.append(_ev("forecast_weather", v["weather"], None, when, src, loc))
    return out


def _check(source: dict | None, name: str) -> str | None:
    """回傳問題描述；None 表示可用。"""
    if source is None:
        return f"缺少{name}"
    st = source.get("status")
    if st == Status.OK.value:
        return None
    msg = source.get("message") or ""
    return {"stale": f"{name}已過期", "missing": f"{name}不可用", "error": f"{name}取得失敗"}.get(st, f"{name}狀態 {st}") \
        + (f"：{msg}" if msg else "")


def _device_known(device_state: dict | None, name: str) -> str:
    if not device_state:
        return "unknown"
    return device_state.get(name, "unknown")


def recommend(scenario: str, indoor: dict | None = None, forecast: dict | None = None,
              device_state: dict | None = None, preference: dict | None = None) -> dict:
    pref = preference or {}
    out = {"type": "recommendation_candidate", "scenario": scenario, "ruleset_version": RULESET_VERSION,
           "status": Status.OK.value, "rules_fired": [], "evidence": [], "limitations": [],
           "missing": [], "clarify": None, "device_state": device_state or {"status": "unknown"}}
    if scenario not in SCENARIOS:
        out.update(status="needs_clarification", clarify="請說明是設備使用、外出，還是曬衣問題。")
        return out

    if scenario == "device":
        prob = _check(indoor, "室內量測")
        if prob:
            out.update(status="insufficient_data", missing=[prob])
            return out
        out["evidence"] = _indoor_evidence(indoor)
        T = indoor["latest"]["temperature_c"]
        RH = indoor["latest"]["relative_humidity_pct"]
        rh_thr = RULES["DK-01"]["threshold"]["relative_humidity_pct"]
        t_thr = pref.get("comfort_temp_max", RULES["DK-03"]["threshold"]["temperature_c"])
        tr = indoor.get("trend") or {}
        dehu = _device_known(device_state, "dehumidifier")
        ac = _device_known(device_state, "ac")
        if RH >= rh_thr:
            if dehu == "on":
                s = f"室內濕度 {RH:.0f}%，除濕機已確認開啟，可再觀察一段時間。"
            else:
                s = f"室內濕度 {RH:.0f}%（≥{rh_thr}%），可考慮使用除濕機或改善通風。"
                if dehu == "unknown":
                    s += "目前不確定除濕機狀態。"
            out["rules_fired"].append(_fire("DK-01", s, out["evidence"][:2]))
        if T >= t_thr:
            s = (f"室內溫度 {T:.1f}°C，冷氣已確認開啟。" if ac == "on"
                 else f"室內溫度 {T:.1f}°C（≥{t_thr}°C），若覺得悶熱可考慮使用冷氣或電扇。")
            out["rules_fired"].append(_fire("DK-03", s, out["evidence"][:2]))
        if tr and tr.get("delta_temperature_c", 0) > 0.5 and tr.get("delta_relative_humidity_pct", 0) < -2:
            out["rules_fired"].append(_fire(
                "DK-02", "近期溫度上升、相對濕度下降；這可能只是升溫造成的相對濕度變化。",
                out["evidence"][2:]))
        if not out["rules_fired"]:
            out["note"] = f"室內 {T:.1f}°C、{RH:.0f}%，未達任何規則門檻，暫不需要調整設備。"

    elif scenario == "outing":
        prob = _check(forecast, "目的地預報")
        if prob:
            if forecast is not None and forecast.get("status") == Status.MISSING.value and "時段" in (forecast.get("message") or ""):
                out.update(status="needs_clarification", clarify="請提供外出的行政區與預計時段。", missing=[prob])
            else:
                out.update(status="insufficient_data", missing=[prob])
            return out
        out["evidence"] = _forecast_evidence(forecast)
        v = forecast["values"]
        pop = (v.get("pop_pct") or {}).get("max")
        th4 = RULES["DK-04"]["threshold"]
        if pop is None:
            out["missing"].append("目標時段沒有降雨機率")
        elif pop >= th4["pop_pct"]:
            s = (f"降雨機率最高 {pop:.0f}%，建議帶傘並預留交通時間。" if pop >= th4["pop_pct_high"]
                 else f"降雨機率最高 {pop:.0f}%，可攜帶折傘。")
            out["rules_fired"].append(_fire("DK-04", s, [e for e in out["evidence"] if e["kind"] == "forecast_pop_max"]))
        t = v.get("temperature_c")
        th6 = RULES["DK-06"]["threshold"]
        if t:
            tev = [e for e in out["evidence"] if e["kind"] == "forecast_temperature"]
            if t["max"] >= th6["temperature_c_hot"]:
                out["rules_fired"].append(_fire("DK-06", f"預報最高 {t['max']:.0f}°C，注意防曬與補水。", tev))
            elif t["min"] <= th6["temperature_c_cold"]:
                out["rules_fired"].append(_fire("DK-06", f"預報最低 {t['min']:.0f}°C，建議帶外套。", tev))

    elif scenario == "laundry":
        p_in, p_out = _check(indoor, "室內量測"), _check(forecast, "曬衣地點預報")
        out["missing"] = [p for p in (p_in, p_out) if p]
        if p_in and p_out:
            out["status"] = "insufficient_data"
            return out
        if not p_in:
            out["evidence"] += _indoor_evidence(indoor)
        if not p_out:
            out["evidence"] += _forecast_evidence(forecast)
        pop = None if p_out else ((forecast["values"].get("pop_pct") or {}).get("max"))
        rh_in = None if p_in else indoor["latest"]["relative_humidity_pct"]
        rh_out = None if p_out else ((forecast["values"].get("relative_humidity_pct") or {}).get("mean"))
        if pop is not None:
            ev = [e for e in out["evidence"] if e["kind"] in ("forecast_pop_max", "forecast_relative_humidity")]
            if pop >= RULES["DK-04"]["threshold"]["pop_pct"]:
                out["rules_fired"].append(_fire("DK-04", f"曬衣時段降雨機率最高 {pop:.0f}%，不建議戶外曬衣。", ev))
            else:
                out["rules_fired"].append(_fire("DK-04", f"曬衣時段降雨機率最高 {pop:.0f}%，戶外曬衣可列入選項。", ev))
        if rh_in is not None:
            ev = [e for e in out["evidence"] if e["kind"].startswith("indoor_")]
            thr = RULES["DK-05"]["threshold"]["relative_humidity_pct"]
            dehu = _device_known(device_state, "dehumidifier")
            if rh_in >= thr:
                s = f"室內濕度 {rh_in:.0f}%（≥{thr}%），室內晾衣建議搭配除濕機或通風。"
                if dehu == "on":
                    s = f"室內濕度 {rh_in:.0f}%，除濕機已確認開啟，室內晾衣可行。"
            else:
                s = f"室內濕度 {rh_in:.0f}%，室內晾衣條件尚可，保持通風。"
            if rh_out is not None:
                s += f" 預報戶外平均濕度 {rh_out:.0f}%。"
            out["rules_fired"].append(_fire("DK-05", s, ev))
        if out["missing"]:
            out["status"] = "partial"
            out["limitations"].append("只取得部分資料，僅能就可用的一側提供建議：" + "；".join(out["missing"]))

    for r in out["rules_fired"]:
        for lim in r["limitations"]:
            if lim not in out["limitations"]:
                out["limitations"].append(lim)
    return out
