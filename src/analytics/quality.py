"""量測資料品質檢查與有效視窗切分（B 讀取 A 的量測）。

A 的量測查詢介面回傳的每筆資料至少要有：
    room_id, measured_at, temperature_c, relative_humidity_pct, quality_flag

本模組再做一次範圍檢查，並在時間缺口處切段；趨勢與事件只在同一有效段內計算，
斷線恢復後的第一筆不會和斷線前的讀值相減，因此「時間缺口不視為設備操作」。
"""
from __future__ import annotations

import sqlite3
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from typing import Iterable

from src.common.adapters import measurement_from_a
from src.common.schema import (MEASUREMENT_GAP_MIN, MEASUREMENT_MAX_AGE_MIN, QualityFlag,
                               fmt_time, fmt_utc_ms, normalize_quality_flag, parse_time)

TEMP_RANGE = (-10.0, 60.0)
RH_RANGE = (0.0, 100.0)


@dataclass
class Point:
    t: datetime
    temperature_c: float
    relative_humidity_pct: float


@dataclass
class QualityReport:
    total: int = 0
    valid: int = 0
    invalid: dict = field(default_factory=dict)          # 原因 → 筆數
    gaps: list = field(default_factory=list)             # [{from, to, minutes}]
    duplicates: int = 0

    def as_dict(self) -> dict:
        return {"total": self.total, "valid": self.valid, "invalid": self.invalid,
                "duplicates": self.duplicates, "gaps": self.gaps}


def _invalid_reason(m: dict) -> str | None:
    """回傳排除原因，通過檢查回 None。原因名與 A 的 quality_flag 同一組值。"""
    flag = normalize_quality_flag(m.get("quality_flag"))
    if flag != QualityFlag.OK.value:
        return flag
    t, rh = m.get("temperature_c"), m.get("relative_humidity_pct")
    if t is None or rh is None:
        return QualityFlag.MISSING.value
    if not (TEMP_RANGE[0] <= float(t) <= TEMP_RANGE[1]) or not (RH_RANGE[0] <= float(rh) <= RH_RANGE[1]):
        return QualityFlag.INVALID_RANGE.value
    return None


def valid_segments(measurements: Iterable[dict],
                   max_gap: timedelta = timedelta(minutes=MEASUREMENT_GAP_MIN)
                   ) -> tuple[list[list[Point]], QualityReport]:
    """回傳依時間缺口切開的有效段，以及品質報告。"""
    rep = QualityReport()
    pts: list[Point] = []
    seen: set = set()
    for m in measurements:
        rep.total += 1
        reason = _invalid_reason(m)
        if reason:
            rep.invalid[reason] = rep.invalid.get(reason, 0) + 1
            continue
        t = parse_time(m["measured_at"])
        key = m.get("measurement_id") or t      # A §1：measurement_id 為唯一鍵，重送只留一筆
        if key in seen:
            rep.duplicates += 1
            continue
        seen.add(key)
        pts.append(Point(t, float(m["temperature_c"]), float(m["relative_humidity_pct"])))
    pts.sort(key=lambda p: p.t)
    rep.valid = len(pts)
    segs: list[list[Point]] = []
    for p in pts:
        if segs and p.t - segs[-1][-1].t <= max_gap:
            segs[-1].append(p)
        else:
            if segs:
                prev = segs[-1][-1].t
                rep.gaps.append({"from": fmt_time(prev), "to": fmt_time(p.t),
                                 "minutes": round((p.t - prev).total_seconds() / 60, 1)})
            segs.append([p])
    return segs, rep


def load_measurements_sqlite(conn: sqlite3.Connection, room_id: str, start: str | datetime,
                             end: str | datetime) -> list[dict]:
    """暫用：直接讀 A 的 measurements 表；A 的查詢函式確定後改為呼叫它。

    時段為半開區間 [start, end)，與 A 規格 §1 的歷史查詢一致。
    邊界用 fmt_utc_ms() 產生 A 的儲存格式（UTC、毫秒、Z 結尾）；
    用 Asia/Taipei 字串比較會因格式不同而過濾錯誤。
    """
    cur = conn.execute(
        "SELECT measurement_id, room_id, device_id, measured_at, received_at, "
        "temperature_c, relative_humidity_pct, quality_flag "
        "FROM measurements WHERE room_id = ? AND measured_at >= ? AND measured_at < ? ORDER BY measured_at",
        (room_id, fmt_utc_ms(start), fmt_utc_ms(end)))
    cols = [c[0] for c in cur.description]
    return [measurement_from_a(dict(zip(cols, r))) for r in cur.fetchall()]


def indoor_summary(measurements: list[dict], now: datetime,
                   freshness: timedelta = timedelta(minutes=MEASUREMENT_MAX_AGE_MIN),
                   trend_window: timedelta = timedelta(minutes=30)) -> dict:
    """給規則與 C 顯示用的室內現況：最新讀值、新鮮度、近 trend_window 的變化量。"""
    segs, rep = valid_segments(measurements)
    measurements = list(measurements)
    out = {"type": "indoor", "room_id": measurements[0].get("room_id") if measurements else None, "status": "missing", "latest": None, "measured_at": None,
           "age_minutes": None, "trend": None, "quality": rep.as_dict()}
    if not segs:
        return out
    last_seg = segs[-1]
    p = last_seg[-1]
    age = now - p.t
    out.update(latest={"temperature_c": p.temperature_c, "relative_humidity_pct": p.relative_humidity_pct},
               measured_at=fmt_time(p.t), age_minutes=round(age.total_seconds() / 60, 1),
               status="ok" if age <= freshness else "stale")
    win = [q for q in last_seg if q.t >= p.t - trend_window]
    if len(win) >= 2 and (win[-1].t - win[0].t) >= trend_window / 2:
        out["trend"] = {"window_minutes": round((win[-1].t - win[0].t).total_seconds() / 60, 1),
                        "delta_temperature_c": round(win[-1].temperature_c - win[0].temperature_c, 2),
                        "delta_relative_humidity_pct": round(win[-1].relative_humidity_pct - win[0].relative_humidity_pct, 2)}
    return out
