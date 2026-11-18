"""溫濕度持續變化事件判定、事件合併與詢問間隔。

負責人：鍾易憲（工作流 B）

流程（對應 DESIGN.md Algorithm 2 第 01–07 行）
1. quality.valid_segments() 依時間缺口切段；只在段內計算。
2. 段內以 3 點中位數平滑，去掉單點跳動。
3. 每個時間點用過去 slope_window 內的最小平方法求溫度、濕度斜率。
4. 斜率超過門檻且連續 >= min_duration，並且累積變化量 >= min_delta，才成為「候選偵測（run）」；
   成立的那一刻記為 decision_at，所有分類特徵只用 decision_at 以前的資料。
5. 相鄰 run 若間隔 <= merge_gap 合併為同一事件（同一 event_id）。
6. 詢問政策另外決定是否詢問（ask_policy），合併與冷卻間隔可和基礎詢問比較。

所有門檻都在 EventConfig，附版本；第 3 週調參只能用訓練期資料。
"""
from __future__ import annotations

import math
from dataclasses import asdict, dataclass, field
from datetime import datetime, timedelta
from statistics import median
from typing import Iterable

from src.analytics.quality import Point, valid_segments
from src.common.schema import fmt_time, parse_time

DETECTOR_VERSION = "events/1.0.0"


@dataclass(frozen=True)
class EventConfig:
    version: str = "cfg-2026w2-default"
    max_gap_min: float = 5.0            # 超過即切段（斷線）
    slope_window_min: float = 10.0      # 斜率視窗
    temp_slope_thr: float = 0.05        # °C/min（10 分鐘約 0.5°C）
    rh_slope_thr: float = 0.25          # %RH/min（10 分鐘約 2.5%）
    min_duration_min: float = 5.0       # 斜率持續超標的最短時間（自第一個超標判定點起算）
    min_delta_temp: float = 0.8         # 累積變化量門檻 °C
    min_delta_rh: float = 3.0           # 累積變化量門檻 %RH
    merge_gap_min: float = 20.0         # 事件合併間隔

    def as_dict(self) -> dict:
        return asdict(self)


def dew_point_c(t: float, rh: float) -> float:
    """Magnus 公式露點；僅輔助解讀水氣，不作為設備操作證據。"""
    a, b = 17.62, 243.12
    g = math.log(max(rh, 0.1) / 100.0) + a * t / (b + t)
    return round(b * g / (a - g), 2)


def _smooth(seg: list[Point]) -> list[Point]:
    if len(seg) < 3:
        return list(seg)
    out = [seg[0]]
    for i in range(1, len(seg) - 1):
        w = seg[i - 1:i + 2]
        out.append(Point(seg[i].t, median(p.temperature_c for p in w), median(p.relative_humidity_pct for p in w)))
    out.append(seg[-1])
    return out


def _slope(ts: list[float], ys: list[float]) -> float:
    n = len(ts)
    mt, my = sum(ts) / n, sum(ys) / n
    den = sum((t - mt) ** 2 for t in ts)
    return 0.0 if den == 0 else sum((t - mt) * (y - my) for t, y in zip(ts, ys)) / den


def _dir(v: float, thr: float) -> str:
    return "up" if v >= thr else "down" if v <= -thr else "flat"


@dataclass
class Run:
    """一次候選偵測（未合併）。"""
    room_id: str
    start_at: datetime
    decision_at: datetime
    end_at: datetime
    features: dict
    summary: dict = field(default_factory=dict)


def detect_runs(measurements: Iterable[dict], room_id: str, cfg: EventConfig = EventConfig()) -> tuple[list[Run], dict]:
    segs, rep = valid_segments(measurements, timedelta(minutes=cfg.max_gap_min))
    win = timedelta(minutes=cfg.slope_window_min)
    runs: list[Run] = []
    for raw in segs:
        seg = _smooth(raw)
        if not seg:
            continue
        seg_start = seg[0].t
        active_since: int | None = None       # 第一個超標判定點的視窗起點（index）
        first_active_t: datetime | None = None
        cur: Run | None = None
        for i, p in enumerate(seg):
            if p.t - seg_start < win:
                continue                       # 斷線恢復後需先累積一個視窗（暖機）
            j0 = next(k for k in range(i + 1) if seg[k].t > p.t - win)
            w = seg[j0:i + 1]
            if len(w) < 3:
                continue
            ts = [(q.t - w[0].t).total_seconds() / 60 for q in w]
            sT = _slope(ts, [q.temperature_c for q in w])
            sH = _slope(ts, [q.relative_humidity_pct for q in w])
            active = abs(sT) >= cfg.temp_slope_thr or abs(sH) >= cfg.rh_slope_thr
            if not active:
                if cur is not None:
                    cur.end_at = seg[i - 1].t
                    _finish(cur, seg, active_since, i - 1)
                    runs.append(cur)
                cur, active_since, first_active_t = None, None, None
                continue
            if active_since is None:
                active_since, first_active_t = j0, p.t
            if cur is not None:
                cur.end_at = p.t
                continue
            s = seg[active_since]
            dT, dH = p.temperature_c - s.temperature_c, p.relative_humidity_pct - s.relative_humidity_pct
            sustained = p.t - first_active_t >= timedelta(minutes=cfg.min_duration_min)
            if sustained and (abs(dT) >= cfg.min_delta_temp or abs(dH) >= cfg.min_delta_rh):
                dur = p.t - s.t
                feats = {
                    "delta_temperature_c": round(dT, 2),
                    "delta_relative_humidity_pct": round(dH, 2),
                    "slope_temperature_c_per_min": round(sT, 4),
                    "slope_relative_humidity_pct_per_min": round(sH, 4),
                    "start_temperature_c": round(s.temperature_c, 2),
                    "start_relative_humidity_pct": round(s.relative_humidity_pct, 2),
                    "delta_dew_point_c": round(dew_point_c(p.temperature_c, p.relative_humidity_pct)
                                               - dew_point_c(s.temperature_c, s.relative_humidity_pct), 2),
                    "minutes_to_decision": round(dur.total_seconds() / 60, 1),
                    "hour_of_day": p.t.hour,
                    "direction_temperature": _dir(dT, cfg.min_delta_temp / 2),
                    "direction_humidity": _dir(dH, cfg.min_delta_rh / 2),
                }
                cur = Run(room_id, s.t, p.t, p.t, feats)
        if cur is not None:
            cur.end_at = seg[-1].t
            _finish(cur, seg, active_since, len(seg) - 1)
            cur.summary["open_at_segment_end"] = True
            runs.append(cur)
    return runs, rep.as_dict()


def _finish(run: Run, seg: list[Point], i0: int | None, i1: int) -> None:
    """事件結束後的整體變化，只供顯示與報告，不進分類特徵。"""
    if i0 is None:
        return
    a, b = seg[i0], seg[i1]
    run.summary.update(total_delta_temperature_c=round(b.temperature_c - a.temperature_c, 2),
                       total_delta_relative_humidity_pct=round(b.relative_humidity_pct - a.relative_humidity_pct, 2),
                       duration_minutes=round((b.t - a.t).total_seconds() / 60, 1))


def make_event_id(room_id: str, start: datetime) -> str:
    return f"ev-{room_id}-{start.strftime('%Y%m%dT%H%M')}"


def _gap_adjacent(start_at: datetime, gaps: list[dict], cfg: EventConfig) -> bool:
    """事件起點是否緊接在量測缺口之後。

    斜率視窗需要暖機，所以事件不會跨越缺口；但起點落在斷線恢復後
    slope_window + min_duration 之內的事件，基線只有重連後的少數讀值，
    依 A 規格 §3 標為 `gap`，由分析端決定排除或重建視窗。
    """
    near = timedelta(minutes=cfg.slope_window_min + cfg.min_duration_min)
    for g in gaps or []:
        to = parse_time(g.get("to"))
        if to is not None and to <= start_at and start_at - to <= near:
            return True
    return False


def merge_runs(runs: list[Run], cfg: EventConfig = EventConfig(),
               gaps: list[dict] | None = None) -> list[dict]:
    """把間隔 <= merge_gap 的 run 合併成事件。事件特徵取第一個 run（最早可判斷的時間點）。

    `gaps` 為 quality 報告的缺口清單，用來標記 A 規格 §3 的 quality_flag。
    """
    gap = timedelta(minutes=cfg.merge_gap_min)
    events: list[dict] = []
    for r in sorted(runs, key=lambda x: (x.room_id, x.start_at)):
        last = events[-1] if events and events[-1]["room_id"] == r.room_id else None
        if last is not None and r.start_at - last["_end"] <= gap:
            last["_end"] = max(last["_end"], r.end_at)
            last["end_at"] = fmt_time(last["_end"])
            last["run_decisions"].append(fmt_time(r.decision_at))
            last["n_runs"] += 1
            continue
        events.append({
            "type": "event",
            "event_id": make_event_id(r.room_id, r.start_at),
            "room_id": r.room_id,
            "start_at": fmt_time(r.start_at),
            # decision_at 為 B 內部名稱，detected_at 是 A 規格 §3 的欄位名，值相同。
            "decision_at": fmt_time(r.decision_at),
            "detected_at": fmt_time(r.decision_at),
            "end_at": fmt_time(r.end_at),
            "features": dict(r.features),
            "summary": dict(r.summary),
            "n_runs": 1,
            "run_decisions": [fmt_time(r.decision_at)],
            "quality_flag": "gap" if _gap_adjacent(r.start_at, gaps, cfg) else "ok",
            # A 規格 §3：event_version 是整數，初次保存 1，同一事件更新時由呼叫端遞增。
            # B 的偵測器與門檻版本另外帶，不佔用這個欄位。
            "event_version": 1,
            "detector_version": DETECTOR_VERSION,
            "config_version": cfg.version,
            "_end": r.end_at,
        })
    for e in events:
        e.pop("_end")
    return events


def detect_events(measurements: Iterable[dict], room_id: str, cfg: EventConfig = EventConfig()) -> dict:
    """給 C／A 的單一入口：回傳事件清單與品質報告。"""
    runs, quality = detect_runs(measurements, room_id, cfg)
    return {"room_id": room_id, "detector_version": DETECTOR_VERSION, "config": cfg.as_dict(),
            "quality": quality, "runs": len(runs),
            "events": merge_runs(runs, cfg, quality.get("gaps"))}
