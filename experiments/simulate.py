"""合成資料產生器——只用於測試實驗管線，不得當作實驗結果。

產出的每份檔案都帶 data_origin = "synthetic"，報告會在標題標示「合成資料」。
真實實驗請改用 A 匯出的量測、回饋與操作日誌。
"""
from __future__ import annotations

import argparse
import json
import math
import random
from datetime import datetime, timedelta
from pathlib import Path

from src.common.schema import TZ_TAIPEI, fmt_time

# 操作對溫濕度的大致影響（每分鐘、持續分鐘數）——粗略假設，僅為測試形狀
EFFECTS = {
    "ac":           {"dT": -0.12, "dH": -0.15, "mins": (18, 30)},
    "dehumidifier": {"dT": +0.02, "dH": -0.35, "mins": (25, 40)},
    "window":       {"dT": -0.06, "dH": +0.25, "mins": (12, 20)},
    "natural":      {"dT": +0.07, "dH": +0.40, "mins": (12, 20)},   # 煮飯、洗澡等，無設備操作
}


def simulate(days: int = 14, seed: int = 7, start: datetime | None = None, room_id: str = "room1",
             ops_per_day: int = 5, reply_prob: float = 0.7) -> dict:
    rnd = random.Random(seed)
    start = start or datetime(2026, 11, 2, 0, 0, tzinfo=TZ_TAIPEI)
    n = days * 24 * 60
    # 安排操作
    plan = []
    for d in range(days):
        for _ in range(ops_per_day):
            kind = rnd.choices(["ac", "dehumidifier", "window", "natural", "mixed"], [3, 3, 2, 3, 1])[0]
            m = d * 1440 + rnd.randint(7 * 60, 23 * 60)
            plan.append((m, kind))
    plan.sort()
    active = []  # (end_minute, dT, dH)
    ops_log, truth = [], []
    T, H = 26.0, 65.0
    meas = []
    off_until = -1
    pi = 0
    for i in range(n):
        t = start + timedelta(minutes=i)
        while pi < len(plan) and plan[pi][0] == i:
            kind = plan[pi][1]
            kinds = ["ac", "dehumidifier"] if kind == "mixed" else [kind]
            for k in kinds:
                eff = EFFECTS[k]
                dur = rnd.randint(*eff["mins"])
                active.append((i + dur, eff["dT"], eff["dH"]))
            truth.append({"operation_at": fmt_time(t), "kind": kind})
            if kind != "natural" and rnd.random() < 0.9:
                for k in kinds:
                    ops_log.append({"operation_at": fmt_time(t + timedelta(minutes=rnd.randint(0, 3))),
                                    "operation_label": k, "certainty": "sure", "note": "synthetic"})
            elif kind == "natural" and rnd.random() < 0.6:
                ops_log.append({"operation_at": fmt_time(t), "operation_label": "none_confirmed",
                                "certainty": "sure", "note": "synthetic"})
            pi += 1
            # 偶爾斷線
            if rnd.random() < 0.05:
                off_until = i + rnd.randint(8, 30)
        # 日夜緩慢變化 + 回歸
        diurnal = 0.004 * math.sin(2 * math.pi * (i % 1440) / 1440 - math.pi / 2)
        T += diurnal + 0.003 * (26 - T)
        H += -diurnal * 3 + 0.004 * (65 - H)
        for end, dT, dH in active:
            T += dT
            H += dH
        active = [a for a in active if a[0] > i]
        H = min(max(H, 30), 95)
        if i <= off_until:
            continue
        noise_t, noise_h = rnd.gauss(0, 0.05), rnd.gauss(0, 0.2)
        if rnd.random() < 0.002:  # 單點跳動
            noise_t += rnd.choice([-3, 3])
            noise_h += rnd.choice([-10, 10])
        meas.append({"room_id": room_id, "measured_at": fmt_time(t), "temperature_c": round(T + noise_t, 2),
                     "relative_humidity_pct": round(H + noise_h, 1), "quality_flag": "ok"})
    return {"data_origin": "synthetic", "seed": seed, "room_id": room_id, "measurements": meas,
            "op_log": ops_log, "truth": truth, "reply_prob": reply_prob}


def synth_feedback(events: list[dict], truth: list[dict], reply_prob: float, seed: int = 7) -> list[dict]:
    """依合成真值為事件產生回覆（部分不回覆 → 保持 unknown）。"""
    from src.common.schema import parse_time
    rnd = random.Random(seed + 1)
    fb = []
    for e in events:
        if rnd.random() > reply_prob:
            continue
        s, d = parse_time(e["start_at"]) - timedelta(minutes=15), parse_time(e["decision_at"])
        kinds = {x["kind"] for x in truth if s <= parse_time(x["operation_at"]) <= d}
        if "mixed" in kinds or len(kinds - {"natural"}) >= 2:
            lab = "mixed"
        elif kinds - {"natural"}:
            lab = next(iter(kinds - {"natural"}))
        else:
            lab = "none_confirmed"
        fb.append({"event_id": e["event_id"], "asked_at": e["decision_at"],
                   "replied_at": fmt_time(d + timedelta(minutes=rnd.randint(1, 20))),
                   "operation_at": None, "operation_label": lab,
                   "certainty": "sure" if rnd.random() > 0.1 else "unsure"})
    return fb


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--days", type=int, default=14)
    ap.add_argument("--seed", type=int, default=7)
    ap.add_argument("--out", default="experiments/data/synthetic")
    a = ap.parse_args()
    d = simulate(a.days, a.seed)
    out = Path(a.out)
    out.mkdir(parents=True, exist_ok=True)
    for k in ("measurements", "op_log", "truth"):
        (out / f"{k}.json").write_text(json.dumps({"data_origin": "synthetic", "rows": d[k]}, ensure_ascii=False), encoding="utf-8")
    print(f"wrote {len(d['measurements'])} measurements to {out}")


if __name__ == "__main__":
    main()
