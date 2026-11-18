import json
import sqlite3
import urllib.error
from datetime import timedelta
from pathlib import Path

from src.acquisition import cwa_forecast as cf
from src.common.schema import parse_time

FIX = Path(__file__).parent / "fixtures" / "cwa_F-D0047-073_format_sample.json"
NOW = parse_time("2026-11-19T07:00")


def fake_fetcher(payload_bytes):
    calls = []

    def f(url, timeout):
        calls.append(url)
        return payload_bytes
    f.calls = calls
    return f


def ok_fetcher():
    return fake_fetcher(FIX.read_bytes())


def test_ok_values_and_sources(tmp_path):
    f = ok_fetcher()
    r = cf.get_forecast("台中市", "南區", "2026-11-19T09:00", "2026-11-19T15:00", api_key="k", now=NOW,
                        fetcher=f, save_dir=tmp_path)
    assert r["status"] == "ok"
    assert "F-D0047-073" in f.calls[0]
    assert r["location"] == {"county": "臺中市", "district": "南區"}
    assert r["issued_at"] is None                       # 來源沒給就不填
    assert r["retrieved_at"].startswith("2026-11-19T07:00")
    v = r["values"]
    assert v["temperature_c"]["min"] == 24 and v["temperature_c"]["max"] == 26   # 09,12,15 點
    assert v["pop_pct"]["max"] == 20                                                  # 09-12=10, 12-15=20
    assert r["valid_from"].startswith("2026-11-19T06:00")
    assert r["snapshot_id"] and (tmp_path / f"{r['snapshot_id']}.json").exists()


def test_snapshot_replay_and_stale(tmp_path):
    conn = sqlite3.connect(":memory:")
    r = cf.get_forecast("臺中市", "南區", "2026-11-19T09:00", "2026-11-19T12:00", api_key="k", now=NOW,
                        fetcher=ok_fetcher(), save_dir=tmp_path, db=conn)
    assert conn.execute("select count(*) from forecast_snapshots").fetchone()[0] == 1
    snap = tmp_path / f"{r['snapshot_id']}.json"
    fresh = cf.forecast_from_snapshot(snap, "2026-11-19T09:00", "2026-11-19T12:00", now=NOW + timedelta(hours=1))
    assert fresh["status"] == "ok" and fresh["values"] == r["values"]
    old = cf.forecast_from_snapshot(snap, "2026-11-19T09:00", "2026-11-19T12:00", now=NOW + timedelta(hours=7))
    assert old["status"] == "stale" and "新鮮度" in old["message"]
    replay = cf.forecast_from_snapshot(snap, "2026-11-19T09:00", "2026-11-19T12:00", now=NOW + timedelta(days=3), max_age=None)
    assert replay["status"] == "ok" and replay["replay"] is True


def test_target_outside_range():
    r = cf.get_forecast("臺中市", "南區", "2026-11-25T09:00", "2026-11-25T12:00", api_key="k", now=NOW, fetcher=ok_fetcher())
    assert r["status"] == "missing" and "時段" in r["message"]


def test_unknown_district_and_county():
    assert cf.get_forecast("臺中市", "不存在區", "2026-11-19T09:00", "2026-11-19T12:00", api_key="k", now=NOW,
                           fetcher=ok_fetcher())["status"] == "missing"
    assert cf.get_forecast("火星市", "南區", "2026-11-19T09:00", "2026-11-19T12:00", api_key="k", now=NOW,
                           fetcher=ok_fetcher())["status"] == "missing"


def test_county_mismatch_detected():
    r = cf.get_forecast("臺北市", "南區", "2026-11-19T09:00", "2026-11-19T12:00", api_key="k", now=NOW, fetcher=ok_fetcher())
    assert r["status"] in ("missing", "error")


def test_api_failures(monkeypatch):
    monkeypatch.delenv("CWA_API_KEY", raising=False)
    r = cf.get_forecast("臺中市", "南區", "2026-11-19T09:00", "2026-11-19T12:00", now=NOW, fetcher=ok_fetcher())
    assert r["status"] == "error" and "CWA_API_KEY" in r["message"]

    def boom(url, timeout):
        raise urllib.error.URLError("timeout")
    r = cf.get_forecast("臺中市", "南區", "2026-11-19T09:00", "2026-11-19T12:00", api_key="k", now=NOW, fetcher=boom)
    assert r["status"] == "error" and r["values"] is None

    def http401(url, timeout):
        raise urllib.error.HTTPError(url, 401, "Unauthorized", None, None)
    assert "401" in cf.get_forecast("臺中市", "南區", "2026-11-19T09:00", "2026-11-19T12:00", api_key="k",
                                    now=NOW, fetcher=http401)["message"]
    r = cf.get_forecast("臺中市", "南區", "2026-11-19T09:00", "2026-11-19T12:00", api_key="k", now=NOW,
                        fetcher=fake_fetcher(b"<html>"))
    assert r["status"] == "error"
    r = cf.get_forecast("臺中市", "南區", "2026-11-19T09:00", "2026-11-19T12:00", api_key="k", now=NOW,
                        fetcher=fake_fetcher(json.dumps({"success": "false"}).encode()))
    assert r["status"] == "error"


def test_bad_time_window():
    r = cf.get_forecast("臺中市", "南區", "2026-11-19T12:00", "2026-11-19T09:00", api_key="k", now=NOW, fetcher=ok_fetcher())
    assert r["status"] == "missing"


def test_legacy_format_parsing():
    legacy = {"success": "true", "records": {"locations": [{"locationsName": "臺中市", "location": [{
        "locationName": "南區", "weatherElement": [
            {"elementName": "T", "time": [{"dataTime": "2026-11-19 09:00:00", "elementValue": [{"value": "25", "measures": "攝氏度"}]}]},
            {"elementName": "PoP6h", "time": [{"startTime": "2026-11-19 06:00:00", "endTime": "2026-11-19 12:00:00",
                                               "elementValue": [{"value": "40", "measures": "百分比"}]}]}]}]}]}}
    r = cf.build_result(legacy, "臺中市", "南區", parse_time("2026-11-19T08:00"), parse_time("2026-11-19T11:00"), NOW, "x")
    assert r["status"] == "ok"
    assert r["values"]["temperature_c"]["max"] == 25 and r["values"]["pop_pct"]["max"] == 40
    assert "relative_humidity_pct" in r["missing_fields"]
