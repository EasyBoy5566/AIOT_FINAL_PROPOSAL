"""中央氣象署 368 鄉鎮預報（未來 3 天逐 3 小時）查詢工具。

負責人：鍾易憲（工作流 B）

給 C 的主要入口：
    get_forecast(county, district, start, end) -> dict   # 見 docs/interfaces/B_forecast.md

設計重點
- 依「縣市＋行政區＋目標時段」取值，回傳來源、取得時間與適用時段。
- issued_at 只在來源有回傳發布時間時才填，否則為 None，不用推測值。
- 缺資料／API 失敗／快照過期分別以 status = missing / error / stale 回傳，不丟例外給呼叫端。
- 只用標準函式庫；HTTP 取得可注入（fetcher），方便測試與資料回放。

CLI（需環境變數 CWA_API_KEY）：
    python -m src.acquisition.cwa_forecast --county 臺中市 --district 南區 \
        --start 2026-11-19T09:00 --end 2026-11-19T15:00 --save data/forecast_snapshots
"""
from __future__ import annotations

import argparse
import hashlib
import json
import os
import sqlite3
import urllib.error
import urllib.parse
import urllib.request
from datetime import datetime, timedelta
from pathlib import Path
from statistics import mean
from typing import Any, Callable

from src.common.schema import FORECAST_MAX_AGE_HOURS, Status, fmt_time, now_taipei, parse_time

TOOL_VERSION = "cwa_forecast/1.0.0"
API_BASE = "https://opendata.cwa.gov.tw/api/v1/rest/datastore/"
SOURCE_NAME = "中央氣象署開放資料平臺｜鄉鎮天氣預報（未來3天逐3小時）"

# 預報每 6 小時更新（05:30、11:30、17:30、23:30 發布），超過即視為過期。
DEFAULT_MAX_AGE = timedelta(hours=FORECAST_MAX_AGE_HOURS)   # 共用門檻，見 src/common/schema.py

# 各縣市「未來 3 天」鄉鎮預報資料集代碼（一週預報為代碼 +2）。
# 首次真實呼叫時請核對回傳的 LocationsName，若不符請改此表並更新版本。
COUNTY_DATASET_3DAY: dict[str, str] = {
    "宜蘭縣": "F-D0047-001", "桃園市": "F-D0047-005", "新竹縣": "F-D0047-009",
    "苗栗縣": "F-D0047-013", "彰化縣": "F-D0047-017", "南投縣": "F-D0047-021",
    "雲林縣": "F-D0047-025", "嘉義縣": "F-D0047-029", "屏東縣": "F-D0047-033",
    "臺東縣": "F-D0047-037", "花蓮縣": "F-D0047-041", "澎湖縣": "F-D0047-045",
    "基隆市": "F-D0047-049", "新竹市": "F-D0047-053", "嘉義市": "F-D0047-057",
    "臺北市": "F-D0047-061", "高雄市": "F-D0047-065", "新北市": "F-D0047-069",
    "臺中市": "F-D0047-073", "臺南市": "F-D0047-077", "連江縣": "F-D0047-081",
    "金門縣": "F-D0047-085",
}

# ElementValue 鍵 → 本專案欄位
VALUE_KEYS = {
    "Temperature": "temperature_c",
    "RelativeHumidity": "relative_humidity_pct",
    "ProbabilityOfPrecipitation": "pop_pct",
    "DewPoint": "dew_point_c",
    "ApparentTemperature": "apparent_temperature_c",
    "WindSpeed": "wind_speed_ms",
    "Weather": "weather",
    "WeatherCode": "weather_code",
}
NUMERIC_FIELDS = {"temperature_c", "relative_humidity_pct", "pop_pct", "dew_point_c",
                  "apparent_temperature_c", "wind_speed_ms"}
# 舊版格式（elementName 縮寫 + elementValue[].value）相容
LEGACY_ELEMENT = {"T": "temperature_c", "RH": "relative_humidity_pct", "PoP6h": "pop_pct",
                  "PoP12h": "pop_pct", "Td": "dew_point_c", "AT": "apparent_temperature_c",
                  "WS": "wind_speed_ms", "Wx": "weather"}
REQUIRED_FIELDS = ("temperature_c", "relative_humidity_pct", "pop_pct")

Fetcher = Callable[[str, float], bytes]


def normalize_county(name: str) -> str:
    return name.strip().replace("台", "臺")


def _g(d: Any, *keys: str, default: Any = None) -> Any:
    """不分大小寫取鍵（新舊版 API 大小寫不同）。"""
    if not isinstance(d, dict):
        return default
    lower = {k.lower(): v for k, v in d.items()}
    for k in keys:
        if k.lower() in lower:
            return lower[k.lower()]
    return default


def _as_list(x: Any) -> list:
    if x is None:
        return []
    return x if isinstance(x, list) else [x]


def _num(v: Any) -> float | None:
    if v is None:
        return None
    try:
        s = str(v).strip()
        if s in ("", "-", "--", "-99", "-999", "None"):
            return None
        return float(s)
    except ValueError:
        return None


def default_fetcher(url: str, timeout: float) -> bytes:
    req = urllib.request.Request(url, headers={"Accept": "application/json"})
    with urllib.request.urlopen(req, timeout=timeout) as resp:  # noqa: S310 (固定官方網域)
        return resp.read()


# ---------------------------------------------------------------------------
# 解析
# ---------------------------------------------------------------------------

def parse_slots(location: dict) -> list[dict]:
    """把單一行政區的 WeatherElement 攤平成時段清單。

    每筆：{field, start, end, value}；點預報（DataTime）start == end。
    """
    slots: list[dict] = []
    for el in _as_list(_g(location, "WeatherElement")):
        ename = _g(el, "ElementName", default="")
        for t in _as_list(_g(el, "Time")):
            dt = parse_time(_g(t, "DataTime"))
            st = parse_time(_g(t, "StartTime")) or dt
            en = parse_time(_g(t, "EndTime")) or dt
            if st is None:
                continue
            for ev in _as_list(_g(t, "ElementValue")):
                if not isinstance(ev, dict):
                    continue
                if ename in LEGACY_ELEMENT and "value" in {k.lower() for k in ev}:
                    items = [(LEGACY_ELEMENT[ename], _g(ev, "value"))]
                else:
                    items = [(VALUE_KEYS[k], v) for k, v in ev.items() if k in VALUE_KEYS]
                for field, raw in items:
                    val = _num(raw) if field in NUMERIC_FIELDS else (str(raw).strip() or None)
                    if val is None:
                        continue
                    slots.append({"field": field, "start": st, "end": en, "value": val})
    return slots


def find_location(payload: dict, county: str, district: str) -> tuple[dict | None, str | None, str | None]:
    """回傳 (location, county_name_from_source, issued_at_raw)。"""
    records = _g(payload, "records", default={})
    issued = _g(records, "IssueTime", "issueTime") or _g(_g(records, "datasetInfo", default={}), "issueTime")
    for locs in _as_list(_g(records, "Locations", "locations")):
        cname = _g(locs, "LocationsName", "locationsName")
        for loc in _as_list(_g(locs, "Location", "location")):
            if _g(loc, "LocationName", "locationName") == district:
                return loc, cname, issued
    return None, None, issued


def summarize(slots: list[dict], start: datetime, end: datetime) -> tuple[dict, list[dict]]:
    """目標時段內的摘要值與使用到的時段。

    點預報取 start <= t <= end；區間預報取與 [start, end) 重疊者。
    """
    used = []
    for s in slots:
        point = s["start"] == s["end"]
        if (point and start <= s["start"] <= end) or (not point and s["start"] < end and s["end"] > start):
            used.append(s)
    by: dict[str, list] = {}
    for s in used:
        by.setdefault(s["field"], []).append(s["value"])

    def stats(field: str) -> dict | None:
        v = by.get(field)
        if not v:
            return None
        return {"min": min(v), "max": max(v), "mean": round(mean(v), 1)}

    values = {
        "temperature_c": stats("temperature_c"),
        "relative_humidity_pct": stats("relative_humidity_pct"),
        "pop_pct": None if not by.get("pop_pct") else {"max": max(by["pop_pct"])},
        "apparent_temperature_c": stats("apparent_temperature_c"),
        "weather": sorted(set(by.get("weather", []))) or None,
    }
    used_out = [{"field": s["field"], "start": fmt_time(s["start"]), "end": fmt_time(s["end"]),
                 "value": s["value"]} for s in sorted(used, key=lambda x: (x["field"], x["start"]))]
    return values, used_out


# ---------------------------------------------------------------------------
# 主要入口
# ---------------------------------------------------------------------------

def _base(county: str, district: str, start: datetime | None, end: datetime | None,
          dataset_id: str | None, retrieved_at: datetime) -> dict:
    return {
        "type": "forecast",
        "status": Status.ERROR.value,
        "tool_version": TOOL_VERSION,
        "source": SOURCE_NAME,
        "dataset_id": dataset_id,
        "location": {"county": county, "district": district},
        "target": {"start": fmt_time(start), "end": fmt_time(end)},
        "retrieved_at": fmt_time(retrieved_at),
        "issued_at": None,
        "valid_from": None,
        "valid_to": None,
        "values": None,
        "slots_used": [],
        "missing_fields": [],
        "message": None,
        "snapshot_id": None,
    }


def build_result(payload: dict, county: str, district: str, start: datetime, end: datetime,
                 retrieved_at: datetime, dataset_id: str | None) -> dict:
    """由已取得的 API 回應建立結果（即時查詢與快照回放共用）。"""
    res = _base(county, district, start, end, dataset_id, retrieved_at)
    if str(_g(payload, "success", default="true")).lower() != "true":
        res["message"] = f"API 回應 success={_g(payload, 'success')}"
        return res
    loc, src_county, issued = find_location(payload, county, district)
    if loc is None:
        res["status"] = Status.MISSING.value
        res["message"] = f"來源中找不到行政區「{district}」"
        return res
    if src_county and normalize_county(src_county) != county:
        res["status"] = Status.ERROR.value
        res["message"] = f"資料集縣市為「{src_county}」，與查詢的「{county}」不符；請核對 COUNTY_DATASET_3DAY"
        return res
    res["issued_at"] = fmt_time(parse_time(issued)) if issued else None
    res["geocode"] = _g(loc, "Geocode", "geocode")
    slots = parse_slots(loc)
    if not slots:
        res["status"] = Status.MISSING.value
        res["message"] = "該行政區沒有可解析的預報值"
        return res
    res["valid_from"] = fmt_time(min(s["start"] for s in slots))
    res["valid_to"] = fmt_time(max(s["end"] for s in slots))
    values, used = summarize(slots, start, end)
    res["slots_used"] = used
    if not used:
        res["status"] = Status.MISSING.value
        res["message"] = "目標時段不在預報涵蓋範圍內"
        return res
    res["values"] = values
    res["missing_fields"] = [f for f in REQUIRED_FIELDS if values.get(f) is None]
    res["status"] = Status.OK.value
    if res["missing_fields"]:
        res["message"] = "部分欄位在目標時段沒有預報值：" + "、".join(res["missing_fields"])
    return res


def check_freshness(result: dict, now: datetime | None = None,
                    max_age: timedelta = DEFAULT_MAX_AGE) -> dict:
    """快照超過 max_age 時把 status 改為 stale（不改動原值，供呼叫端說明）。"""
    now = now or now_taipei()
    r = dict(result)
    got = parse_time(r.get("retrieved_at"))
    if r.get("status") == Status.OK.value and got is not None and now - got > max_age:
        r["status"] = Status.STALE.value
        age_h = (now - got).total_seconds() / 3600
        r["message"] = f"預報快照取得於 {age_h:.1f} 小時前，超過 {max_age.total_seconds()/3600:.0f} 小時新鮮度時限"
    return r


def get_forecast(county: str, district: str, start: str | datetime, end: str | datetime, *,
                 api_key: str | None = None, now: datetime | None = None,
                 fetcher: Fetcher | None = None, timeout: float = 10.0,
                 save_dir: str | Path | None = None, db: sqlite3.Connection | None = None) -> dict:
    """查詢指定縣市行政區、目標時段的預報。永不丟例外，失敗以 status 表示。"""
    now = now or now_taipei()
    county = normalize_county(county)
    st, en = parse_time(start), parse_time(end)
    dataset_id = COUNTY_DATASET_3DAY.get(county)
    res = _base(county, district, st, en, dataset_id, now)
    if st is None or en is None or en <= st:
        res["message"] = "目標時段不完整或結束早於開始"
        res["status"] = Status.MISSING.value
        return res
    if dataset_id is None:
        res["status"] = Status.MISSING.value
        res["message"] = f"不支援的縣市「{county}」"
        return res
    api_key = api_key or os.environ.get("CWA_API_KEY")
    if not api_key:
        res["message"] = "未設定 CWA_API_KEY"
        return res
    url = API_BASE + dataset_id + "?" + urllib.parse.urlencode({"Authorization": api_key, "format": "JSON"})
    try:
        raw = (fetcher or default_fetcher)(url, timeout)
        payload = json.loads(raw)
    except urllib.error.HTTPError as e:
        res["message"] = f"API HTTP {e.code}"
        return res
    except (urllib.error.URLError, TimeoutError, OSError) as e:
        res["message"] = f"API 連線失敗：{e}"
        return res
    except json.JSONDecodeError:
        res["message"] = "API 回應不是有效 JSON"
        return res
    result = build_result(payload, county, district, st, en, now, dataset_id)
    if result["status"] == Status.OK.value and (save_dir or db is not None):
        result["snapshot_id"] = save_snapshot(result, payload, save_dir=save_dir, db=db)
    return result


# ---------------------------------------------------------------------------
# 快照保存與回放
# ---------------------------------------------------------------------------

SNAPSHOT_DDL = """
CREATE TABLE IF NOT EXISTS forecast_snapshots (
    snapshot_id TEXT PRIMARY KEY,
    location_id TEXT NOT NULL,
    retrieved_at TEXT NOT NULL,
    issued_at TEXT,
    valid_from TEXT,
    valid_to TEXT,
    forecast_payload TEXT NOT NULL,
    source TEXT NOT NULL,
    dataset_id TEXT,
    tool_version TEXT
)"""


def _snapshot_id(dataset_id: str, district: str, retrieved_at: str) -> str:
    h = hashlib.sha1(f"{dataset_id}|{district}|{retrieved_at}".encode()).hexdigest()[:10]
    return f"fc-{retrieved_at[:16].replace(':', '').replace('-', '')}-{h}"


def save_snapshot(result: dict, payload: dict, *, save_dir: str | Path | None = None,
                  db: sqlite3.Connection | None = None) -> str:
    """保存該行政區的原始預報（只存該行政區，避免整縣資料重複）。"""
    loc, _, _ = find_location(payload, result["location"]["county"], result["location"]["district"])
    sid = _snapshot_id(result["dataset_id"] or "", result["location"]["district"], result["retrieved_at"])
    location_id = f"{result['location']['county']}/{result['location']['district']}"
    record = {
        "snapshot_id": sid, "location_id": location_id, "retrieved_at": result["retrieved_at"],
        "issued_at": result["issued_at"], "valid_from": result["valid_from"], "valid_to": result["valid_to"],
        "source": SOURCE_NAME, "dataset_id": result["dataset_id"], "tool_version": TOOL_VERSION,
        "forecast_payload": {"success": "true", "records": {"Locations": [
            {"LocationsName": result["location"]["county"], "Location": [loc]}]}},
    }
    if save_dir:
        p = Path(save_dir)
        p.mkdir(parents=True, exist_ok=True)
        (p / f"{sid}.json").write_text(json.dumps(record, ensure_ascii=False, indent=2), encoding="utf-8")
    if db is not None:
        db.execute(SNAPSHOT_DDL)
        db.execute(
            "INSERT OR REPLACE INTO forecast_snapshots VALUES (?,?,?,?,?,?,?,?,?,?)",
            (sid, location_id, record["retrieved_at"], record["issued_at"], record["valid_from"],
             record["valid_to"], json.dumps(record["forecast_payload"], ensure_ascii=False),
             SOURCE_NAME, record["dataset_id"], TOOL_VERSION))
        db.commit()
    return sid


def forecast_from_snapshot(snapshot: dict | str | Path, start: str | datetime, end: str | datetime,
                           now: datetime | None = None, max_age: timedelta | None = DEFAULT_MAX_AGE) -> dict:
    """以保存的快照回放查詢。max_age=None 表示回放模式不檢查新鮮度（展示時須標明）。"""
    if not isinstance(snapshot, dict):
        snapshot = json.loads(Path(snapshot).read_text(encoding="utf-8"))
    county, district = snapshot["location_id"].split("/", 1)
    payload = snapshot["forecast_payload"]
    if isinstance(payload, str):
        payload = json.loads(payload)
    res = build_result(payload, county, district, parse_time(start), parse_time(end),
                       parse_time(snapshot["retrieved_at"]), snapshot.get("dataset_id"))
    res["snapshot_id"] = snapshot["snapshot_id"]
    if snapshot.get("issued_at"):
        res["issued_at"] = snapshot["issued_at"]
    res["replay"] = True
    return res if max_age is None else check_freshness(res, now, max_age)


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description="查詢中央氣象署鄉鎮預報並保存快照")
    ap.add_argument("--county", required=True)
    ap.add_argument("--district", required=True)
    ap.add_argument("--start", required=True, help="ISO 時間，無時區視為 +08:00")
    ap.add_argument("--end", required=True)
    ap.add_argument("--save", help="快照 JSON 目錄")
    ap.add_argument("--db", help="SQLite 檔（寫入 forecast_snapshots）")
    a = ap.parse_args(argv)
    conn = sqlite3.connect(a.db) if a.db else None
    res = get_forecast(a.county, a.district, a.start, a.end, save_dir=a.save, db=conn)
    print(json.dumps(res, ensure_ascii=False, indent=2))
    return 0 if res["status"] == Status.OK.value else 1


if __name__ == "__main__":
    raise SystemExit(main())
