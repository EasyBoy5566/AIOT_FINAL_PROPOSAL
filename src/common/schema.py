"""共用欄位慣例（第 1 週共同開工項目的 B 方提案版本）。

A、B、C 三方以此檔為準；改欄位時同步更新 docs/interfaces/ 與 examples/。

- 時間：ISO 8601，含時區，統一 Asia/Taipei（+08:00）。
- 溫度：攝氏（°C），欄位名以 `_c` 結尾。
- 相對濕度：百分比（0–100），欄位名以 `_pct` 結尾。
- 狀態：見 Status；缺值一律以 None 表示，不以 0、-1 或推測值填補。
"""
from __future__ import annotations

from datetime import datetime, timedelta, timezone
from enum import Enum

TZ_TAIPEI = timezone(timedelta(hours=8), name="Asia/Taipei")


class Status(str, Enum):
    """資料或結果的狀態。四種介面（量測、預報、事件、回覆）共用。"""

    OK = "ok"              # 資料有效且在新鮮度時限內
    MISSING = "missing"    # 來源沒有提供（例如目標時段超出預報範圍、房間沒有量測）
    STALE = "stale"        # 有資料但超過新鮮度時限
    UNKNOWN = "unknown"    # 無法判定（例如設備狀態未確認、使用者未回覆）
    ERROR = "error"        # 取得過程失敗（例如 API 逾時、金鑰錯誤）


class QualityFlag(str, Enum):
    """單筆量測的品質旗標（A 寫入、B 讀取）。"""

    OK = "ok"
    OUT_OF_RANGE = "out_of_range"
    MISSING_VALUE = "missing_value"
    SENSOR_ERROR = "sensor_error"


# 事件／回饋的操作標記（第 2 週與 A、C 對齊）
class OperationLabel(str, Enum):
    AC = "ac"                        # 冷氣
    DEHUMIDIFIER = "dehumidifier"    # 除濕機
    WINDOW = "window"                # 開窗／關窗
    MIXED = "mixed"                  # 同時多種操作，保留不拆成單一標籤
    NONE_CONFIRMED = "none_confirmed"  # 使用者明確表示沒有操作
    UNKNOWN = "unknown"              # 未回覆、不確定或無法連到事件


OPERATED_LABELS = {OperationLabel.AC, OperationLabel.DEHUMIDIFIER, OperationLabel.WINDOW, OperationLabel.MIXED}


def to_binary(label: str | OperationLabel) -> int | None:
    """有操作=1、明確無操作=0、未知=None（排除，不當作負類）。"""
    lab = OperationLabel(label)
    if lab in OPERATED_LABELS:
        return 1
    if lab is OperationLabel.NONE_CONFIRMED:
        return 0
    return None


def parse_time(value: str | datetime | None) -> datetime | None:
    """解析 ISO 8601；沒有時區的字串視為 Asia/Taipei。"""
    if value is None or value == "":
        return None
    if isinstance(value, datetime):
        dt = value
    else:
        s = str(value).strip().replace(" ", "T", 1)
        if s.endswith("Z"):
            s = s[:-1] + "+00:00"
        dt = datetime.fromisoformat(s)
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=TZ_TAIPEI)
    return dt.astimezone(TZ_TAIPEI)


def fmt_time(dt: datetime | None) -> str | None:
    return None if dt is None else dt.astimezone(TZ_TAIPEI).isoformat(timespec="seconds")


def now_taipei() -> datetime:
    return datetime.now(TZ_TAIPEI)
