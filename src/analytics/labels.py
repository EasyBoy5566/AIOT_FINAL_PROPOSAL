"""事件標記：把使用者回覆（feedback）與主動操作日誌連到事件。

欄位依《室內感測與回饋資料格式 v1》（主責 高群軒）§2、§4；
A 的 `none` 與 B 內部的 `none_confirmed` 在 src/common/adapters.py 轉換。

標記方式（第 2 週與 A、C 對齊）
- 回覆或日誌只有一種操作 → ac / dehumidifier / window
- 同一事件有兩種以上操作 → mixed（不拆成單一標籤）
- 使用者明確回覆「沒有操作」，或日誌的無操作觀察時段涵蓋事件 → none_confirmed
- 使用者回覆了但不屬於上列 → other（排除在分類外，單獨計數）
- 未回覆、逾時、不確定、找不到任何紀錄 → unknown（不當作「無操作」）

回覆取用順序（A 規格 §4）
1. 同一 feedback_id 的多個版本取 feedback_version 最大者（更正留最新版）。
2. 同一 event_id 可有多次詢問：優先取 status=confirmed 者，再以 replied_at 最晚為準；
   都沒有 confirmed 時取最後一次詢問，標記為 unknown。
"""
from __future__ import annotations

from datetime import timedelta

from src.common.adapters import feedback_from_a
from src.common.schema import (
    LABELLED_FEEDBACK_STATUSES,
    FeedbackStatus,
    OperationLabel,
    normalize_operation_label,
    parse_time,
)

LABEL_VERSION = "labels/1.1.0"
DEVICE_LABELS = {OperationLabel.AC.value, OperationLabel.DEHUMIDIFIER.value, OperationLabel.WINDOW.value}
_EPOCH = parse_time("1970-01-01")


def _combine(labels: set[str]) -> str:
    dev = labels & DEVICE_LABELS
    if OperationLabel.MIXED.value in labels or len(dev) >= 2:
        return OperationLabel.MIXED.value
    if len(dev) == 1:
        return next(iter(dev))
    if OperationLabel.NONE_CONFIRMED.value in labels:
        return OperationLabel.NONE_CONFIRMED.value
    if OperationLabel.OTHER.value in labels:
        return OperationLabel.OTHER.value
    return OperationLabel.UNKNOWN.value


def _version_key(f: dict) -> tuple:
    """同一 feedback_id 的版本排序：feedback_version 優先，其次 updated_at、replied_at。"""
    v = f.get("feedback_version")
    return (int(v) if v is not None else -1,
            parse_time(f.get("updated_at")) or _EPOCH,
            parse_time(f.get("replied_at")) or _EPOCH)


def _choice_key(f: dict) -> tuple:
    """同一 event_id 的多次詢問排序：confirmed 優先，其次回覆時間、詢問時間。"""
    return (1 if f.get("status") in LABELLED_FEEDBACK_STATUSES else 0,
            parse_time(f.get("replied_at")) or _EPOCH,
            parse_time(f.get("asked_at")) or _EPOCH)


def latest_feedback(feedback: list[dict]) -> dict[str, dict]:
    """每個 event_id 取應採用的那一筆回覆（處理重送、更正與多次詢問）。"""
    rows = [feedback_from_a(f) for f in feedback or []]

    # 1. 同一 feedback_id 只留版本最大者
    by_fid: dict[str, dict] = {}
    loose: list[dict] = []
    for f in rows:
        fid = f.get("feedback_id")
        if fid is None:
            loose.append(f)                  # 未帶 feedback_id 的舊資料，直接進第 2 步
            continue
        cur = by_fid.get(fid)
        if cur is None or _version_key(f) >= _version_key(cur):
            by_fid[fid] = f

    # 2. 每個 event_id 取 confirmed 優先、回覆最晚者
    best: dict[str, dict] = {}
    for f in list(by_fid.values()) + loose:
        eid = f.get("event_id")
        if not eid:
            continue
        cur = best.get(eid)
        if cur is None or _choice_key(f) >= _choice_key(cur):
            best[eid] = f
    return best


def _op_log_hits(event: dict, op_log: list[dict], before: timedelta, after: timedelta) -> set[str]:
    """找出可用於標記此事件的日誌紀錄。

    - 房間不同的紀錄不採用（A §2 保留 room_id，單房間時通常相同）。
    - 設備操作：operation_at 落在 [start_at - before, decision_at + after] 內。
    - 無操作觀察：observation_end_at 須存在且觀察時段「涵蓋」[start_at, decision_at]；
      只有部分重疊時無法確認整段都沒有操作，因此不採用。
      舊資料沒有 observation_end_at 時退回單點比對。
    """
    room = event.get("room_id")
    s = parse_time(event["start_at"])
    d = parse_time(event.get("decision_at") or event.get("detected_at"))
    lo, hi = s - before, d + after
    hits: set[str] = set()
    for o in op_log or []:
        if room is not None and o.get("room_id") is not None and o["room_id"] != room:
            continue
        if o.get("certainty", "sure") != "sure":      # B 舊格式的不確定紀錄不採用
            continue
        t0 = parse_time(o.get("operation_at"))
        if t0 is None:
            continue
        label = normalize_operation_label(o.get("operation_label"))
        t1 = parse_time(o.get("observation_end_at"))
        if label == OperationLabel.NONE_CONFIRMED.value and t1 is not None:
            if t0 <= s and t1 >= d:
                hits.add(label)
            continue
        if lo <= t0 <= hi:
            hits.add(label)
    return hits


def link_labels(events: list[dict], feedback: list[dict] | None = None, op_log: list[dict] | None = None,
                before: timedelta = timedelta(minutes=15), after: timedelta = timedelta(minutes=5)) -> list[dict]:
    """為每個事件加上 label、label_source、label_status。不修改傳入物件。

    日誌時間窗：[start_at - before, decision_at + after]；只用判斷當下附近的操作，
    避免拿事件後很久的操作當標記。
    """
    fb = latest_feedback(feedback or [])
    out = []
    for e in events:
        e = dict(e)
        f = fb.get(e["event_id"])
        if f is not None:
            e["label"] = OperationLabel(f["operation_label"]).value
            e["label_source"] = "feedback"
            e["label_status"] = f.get("status") or (
                FeedbackStatus.CONFIRMED.value if f.get("replied_at") else FeedbackStatus.PENDING.value)
            e["feedback_id"] = f.get("feedback_id")
            if f.get("is_late"):
                e["feedback_late"] = True
        else:
            hits = _op_log_hits(e, op_log or [], before, after)
            e["label"] = _combine(hits)
            e["label_source"] = "op_log" if hits else "none"
            e["label_status"] = None
        e["label_version"] = LABEL_VERSION
        out.append(e)
    return out


def label_counts(events: list[dict]) -> dict[str, int]:
    c: dict[str, int] = {}
    for e in events:
        c[e.get("label", "unknown")] = c.get(e.get("label", "unknown"), 0) + 1
    return dict(sorted(c.items()))
