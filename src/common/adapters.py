"""A ↔ B 的邊界轉換（《室內感測與回饋資料格式 v1》，主責 高群軒）。

B 內部的欄位名與標記值不隨 A 改版而散落在各模組，所有差異集中在這一層：

| 項目 | A v1 | B 內部 |
| --- | --- | --- |
| 時間 | UTC `YYYY-MM-DDTHH:mm:ss.SSSZ` | Asia/Taipei，秒精度 |
| 品質旗標 | `ok` / `missing` / `invalid_range` | 同左 |
| 操作標記（無操作） | `none` | `none_confirmed` |
| 操作標記（未知） | 欄位為 `null` | `unknown` |
| 回覆狀態 | `status` 四值 | 只有 `confirmed` 提供標記 |
| 事件判定時間 | `detected_at` | `decision_at` |
| 事件版本 | `event_version`（整數，遞增） | `detector_version` / `config_version`（字串） |

A 的欄位若缺少，一律回 None，不填推測值。
"""
from __future__ import annotations

from src.common.schema import (
    FEATURES_VERSION,
    LABELLED_FEEDBACK_STATUSES,
    FeedbackStatus,
    OperationLabel,
    fmt_utc_ms,
    normalize_operation_label,
    normalize_quality_flag,
    parse_time,
)


def measurement_from_a(row: dict) -> dict:
    """A §1 的量測紀錄 → B 讀取的欄位。保留 measurement_id 供去重與追溯。"""
    return {
        "measurement_id": row.get("measurement_id"),
        "room_id": row.get("room_id"),
        "device_id": row.get("device_id"),
        "measured_at": row.get("measured_at"),
        "received_at": row.get("received_at"),
        "temperature_c": row.get("temperature_c"),
        "relative_humidity_pct": row.get("relative_humidity_pct"),
        "quality_flag": normalize_quality_flag(row.get("quality_flag")),
    }


def feedback_from_a(row: dict) -> dict:
    """A §4 的詢問與回覆 → B 讀取的欄位。

    只有 `status == confirmed` 才帶操作標記；pending／uncertain／expired 一律 unknown
    （A 規格 §4「評估用途」欄）。晚到回覆以 replied_at > expires_at 判定。
    """
    status = row.get("status")
    if status is not None:
        status = FeedbackStatus(status).value
    replied_at, expires_at = parse_time(row.get("replied_at")), parse_time(row.get("expires_at"))
    if status in LABELLED_FEEDBACK_STATUSES:
        label = normalize_operation_label(row.get("operation_label"))
    elif status is not None:
        label = OperationLabel.UNKNOWN.value
    elif row.get("certainty") == "unsure" or replied_at is None:
        # 未帶 status 的 B 舊格式：不確定或尚未回覆一律 unknown
        label = OperationLabel.UNKNOWN.value
    else:
        label = normalize_operation_label(row.get("operation_label"))
    return {
        "feedback_id": row.get("feedback_id"),
        "event_id": row.get("event_id"),
        "asked_at": row.get("asked_at"),
        "expires_at": row.get("expires_at"),
        "replied_at": row.get("replied_at"),
        "operation_at": row.get("operation_at"),
        "operation_label": label,
        "status": status,
        "feedback_version": row.get("feedback_version"),
        "updated_at": row.get("updated_at"),
        "is_late": bool(replied_at and expires_at and replied_at > expires_at),
    }


def op_log_from_a(row: dict) -> dict:
    """A §2 的主動操作日誌 → B 讀取的欄位。

    無操作紀錄是一個觀察時段（operation_at → observation_end_at）；設備操作為單一時間點。
    """
    return {
        "operation_id": row.get("operation_id"),
        "room_id": row.get("room_id"),
        "operation_at": row.get("operation_at"),
        "observation_end_at": row.get("observation_end_at"),
        "operation_label": normalize_operation_label(row.get("operation_label")),
        "action": row.get("action"),
        "recorded_at": row.get("recorded_at"),
    }


def event_to_a(event: dict, event_version: int = 1) -> dict:
    """B 的事件 → A §3 的保存格式。

    - 時間轉成 A 的 UTC 格式。
    - `decision_at` 對應 A 的 `detected_at`。
    - 事件仍持續時（`summary.open_at_segment_end`）`end_at` 回 null，不填最後一筆的時間。
    - `event_version` 為整數，初次保存 1，同一事件更新時由呼叫端遞增。
    - B 自己的偵測器與門檻版本放在 `features._version` 與回傳的 detector/config 欄位，
      不佔用 A 的 `event_version`。
    """
    ongoing = bool(event.get("summary", {}).get("open_at_segment_end"))
    features = dict(event.get("features") or {})
    features["_version"] = FEATURES_VERSION
    return {
        "event_id": event["event_id"],
        "room_id": event["room_id"],
        "start_at": fmt_utc_ms(event["start_at"]),
        "end_at": None if ongoing else fmt_utc_ms(event.get("end_at")),
        "detected_at": fmt_utc_ms(event.get("detected_at") or event["decision_at"]),
        "features": features,
        "quality_flag": event.get("quality_flag", "ok"),
        "event_version": int(event_version),
    }


__all__ = ["measurement_from_a", "feedback_from_a", "op_log_from_a", "event_to_a"]
