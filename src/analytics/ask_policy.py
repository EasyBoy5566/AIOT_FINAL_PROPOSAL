"""詢問政策：決定候選事件何時向使用者詢問。

- baseline：每一次候選偵測（run）都詢問，不合併、不冷卻。
- merged_cooldown：同一事件只問一次，且距上次詢問需超過 cooldown。
  被冷卻擋下的事件仍保留（ask_status = suppressed_cooldown），回覆維持 unknown。

兩者在同一批 run 上回放，用於第 3 週的詢問負擔比較。
"""
from __future__ import annotations

from dataclasses import dataclass
from datetime import timedelta

from src.analytics.events import EventConfig, Run, make_event_id, merge_runs
from src.common.schema import fmt_time, parse_time

POLICY_VERSION = "ask/1.0.0"


@dataclass(frozen=True)
class AskConfig:
    cooldown_min: float = 30.0


def should_ask(event: dict, last_asked_at: str | None, cfg: AskConfig = AskConfig()) -> dict:
    """線上使用（C 呼叫）：給一個新事件與該房間上次詢問時間，回傳是否詢問。"""
    dec = parse_time(event["decision_at"])
    last = parse_time(last_asked_at)
    if last is not None and dec - last < timedelta(minutes=cfg.cooldown_min):
        return {"event_id": event["event_id"], "ask": False, "ask_status": "suppressed_cooldown",
                "next_allowed_at": fmt_time(last + timedelta(minutes=cfg.cooldown_min)),
                "policy_version": POLICY_VERSION}
    return {"event_id": event["event_id"], "ask": True, "ask_status": "asked", "asked_at": fmt_time(dec),
            "policy_version": POLICY_VERSION}


def replay_baseline(runs: list[Run]) -> list[dict]:
    """每個 run 都問一次；用 run 所屬的合併事件 id 記錄，方便計算重複詢問。"""
    events = merge_runs(runs)
    asks = []
    for e in events:
        for d in e["run_decisions"]:
            asks.append({"event_id": e["event_id"], "asked_at": d})
    return asks


def replay_merged_cooldown(runs: list[Run], cfg: AskConfig = AskConfig(),
                           ev_cfg: EventConfig = EventConfig()) -> tuple[list[dict], list[dict]]:
    events = merge_runs(runs, ev_cfg)
    asks, suppressed = [], []
    last_by_room: dict[str, str | None] = {}
    for e in sorted(events, key=lambda x: x["decision_at"]):
        d = should_ask(e, last_by_room.get(e["room_id"]), cfg)
        if d["ask"]:
            asks.append({"event_id": e["event_id"], "asked_at": d["asked_at"]})
            last_by_room[e["room_id"]] = d["asked_at"]
        else:
            suppressed.append({"event_id": e["event_id"], "decision_at": e["decision_at"]})
    return asks, suppressed


__all__ = ["AskConfig", "should_ask", "replay_baseline", "replay_merged_cooldown", "make_event_id"]
