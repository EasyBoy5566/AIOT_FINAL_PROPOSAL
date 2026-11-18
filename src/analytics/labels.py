"""事件標記：把使用者回覆（feedback）與共用操作日誌連到事件。

標記方式（第 2 週與 A、C 對齊）
- 回覆或日誌只有一種操作 → ac / dehumidifier / window
- 同一事件有兩種以上操作 → mixed（不拆成單一標籤）
- 使用者明確回覆「沒有操作」，或日誌在該時段記錄「無操作確認」→ none_confirmed
- 未回覆、回覆不確定、找不到任何紀錄 → unknown（不當作「無操作」）
- 更正：同一 event_id 有多筆回覆時，以 replied_at 最晚的一筆為準

feedback 欄位（A 的回饋表）：event_id, asked_at, replied_at, operation_at, operation_label, certainty
操作日誌欄位（第 1 週起的共用日誌）：operation_at, operation_label, certainty, note
certainty：sure / unsure；unsure 視為 unknown。
"""
from __future__ import annotations

from datetime import timedelta

from src.common.schema import OperationLabel, parse_time

LABEL_VERSION = "labels/1.0.0"
DEVICE_LABELS = {OperationLabel.AC.value, OperationLabel.DEHUMIDIFIER.value, OperationLabel.WINDOW.value}


def _combine(labels: set[str]) -> str:
    dev = labels & DEVICE_LABELS
    if OperationLabel.MIXED.value in labels or len(dev) >= 2:
        return OperationLabel.MIXED.value
    if len(dev) == 1:
        return next(iter(dev))
    if OperationLabel.NONE_CONFIRMED.value in labels:
        return OperationLabel.NONE_CONFIRMED.value
    return OperationLabel.UNKNOWN.value


def latest_feedback(feedback: list[dict]) -> dict[str, dict]:
    """每個 event_id 取最後一次回覆（處理重送與更正）。"""
    best: dict[str, dict] = {}
    for f in feedback:
        eid = f.get("event_id")
        if not eid:
            continue
        cur = best.get(eid)
        if cur is None or (parse_time(f.get("replied_at")) or parse_time("1970-01-01")) >= \
                (parse_time(cur.get("replied_at")) or parse_time("1970-01-01")):
            best[eid] = f
    return best


def link_labels(events: list[dict], feedback: list[dict] | None = None, op_log: list[dict] | None = None,
                before: timedelta = timedelta(minutes=15), after: timedelta = timedelta(minutes=5)) -> list[dict]:
    """為每個事件加上 label、label_source。不修改傳入物件。

    日誌時間窗：[start_at - before, decision_at + after]；只用到判斷當下附近，避免拿事件後很久的操作當標記。
    """
    fb = latest_feedback(feedback or [])
    out = []
    for e in events:
        e = dict(e)
        f = fb.get(e["event_id"])
        if f is not None:
            lab = f.get("operation_label") or OperationLabel.UNKNOWN.value
            if f.get("certainty") == "unsure" or f.get("replied_at") is None:
                lab = OperationLabel.UNKNOWN.value
            e["label"], e["label_source"] = OperationLabel(lab).value, "feedback"
        else:
            s = parse_time(e["start_at"]) - before
            t = parse_time(e["decision_at"]) + after
            hits = {o["operation_label"] for o in (op_log or [])
                    if o.get("certainty", "sure") == "sure" and s <= parse_time(o["operation_at"]) <= t}
            e["label"] = _combine(hits)
            e["label_source"] = "op_log" if hits else "none"
        e["label_version"] = LABEL_VERSION
        out.append(e)
    return out


def label_counts(events: list[dict]) -> dict[str, int]:
    c: dict[str, int] = {}
    for e in events:
        c[e.get("label", "unknown")] = c.get(e.get("label", "unknown"), 0) + 1
    return dict(sorted(c.items()))
