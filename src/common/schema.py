"""共用欄位慣例（第 1 週共同開工項目）。

A、B、C 三方以此檔為準；改欄位時同步更新 docs/interfaces/ 與 examples/。

- 時間：ISO 8601，含時區。B 內部與對外輸出用 Asia/Taipei（+08:00）；
  A 的儲存格式為 UTC `YYYY-MM-DDTHH:mm:ss.SSSZ`，`parse_time()` 兩者都收，
  需要產生 A 格式時用 `fmt_utc_ms()`。
- 溫度：攝氏（°C），欄位名以 `_c` 結尾。
- 相對濕度：百分比（0–100），欄位名以 `_pct` 結尾；差值為百分點。
- 狀態：見 Status；缺值一律以 None 表示，不以 0、-1 或推測值填補。

欄位值以《室內感測與回饋資料格式 v1》（主責 高群軒）為準。B 內部沿用較明確的
`none_confirmed`，在邊界用 normalize_* / to_a_* 轉換，對照表見
docs/interfaces/B_interfaces.md §0。
"""
from __future__ import annotations

from datetime import datetime, timedelta, timezone
from enum import Enum

TZ_TAIPEI = timezone(timedelta(hours=8), name="Asia/Taipei")

SCHEMA_VERSION_A = 1          # 對接的 A 規格版本（measurements payload 的 schema_version）
FEATURES_VERSION = "features/1.0.0"   # A 的 events.features 欄位字典版本（由 B 提供）


class Status(str, Enum):
    """資料或結果的狀態。四種介面（量測、預報、事件、回覆）共用。"""

    OK = "ok"              # 資料有效且在新鮮度時限內
    MISSING = "missing"    # 來源沒有提供（例如目標時段超出預報範圍、房間沒有量測）
    STALE = "stale"        # 有資料但超過新鮮度時限
    UNKNOWN = "unknown"    # 無法判定（例如設備狀態未確認、使用者未回覆）
    ERROR = "error"        # 取得過程失敗（例如 API 逾時、金鑰錯誤）


class QualityFlag(str, Enum):
    """單筆量測的品質旗標（A 寫入、B 讀取）。值依 A 規格 §1。"""

    OK = "ok"                        # 兩值通過檢查
    MISSING = "missing"              # 有空值
    INVALID_RANGE = "invalid_range"  # 量測超出合理範圍


# B 早期自用的旗標名 → A v1 的值。不在表內的值原樣保留：一樣排除，
# 但在品質報告中以原名計數，不靜默改名。
QUALITY_FLAG_ALIASES = {
    "missing_value": QualityFlag.MISSING.value,
    "out_of_range": QualityFlag.INVALID_RANGE.value,
}


def normalize_quality_flag(value: str | None) -> str:
    """A 或 B 的品質旗標 → 統一名稱；未知值原樣回傳（仍視為非 ok）。"""
    if value is None or value == "":
        return QualityFlag.OK.value
    v = str(value)
    return QUALITY_FLAG_ALIASES.get(v, v)


class FeedbackStatus(str, Enum):
    """詢問與回覆的狀態（C 產生、A 保存、B 讀取）。值依 A 規格 §4。"""

    PENDING = "pending"      # 等待回覆
    CONFIRMED = "confirmed"  # 已確認，可納入標記候選
    UNCERTAIN = "uncertain"  # 使用者有回覆但不確定
    EXPIRED = "expired"      # 逾時未回覆


# 只有 confirmed 能提供操作標記，其餘一律 unknown（A 規格 §4 的「評估用途」欄）。
LABELLED_FEEDBACK_STATUSES = {FeedbackStatus.CONFIRMED.value}

# 使用者實際回覆過的狀態（有 replied_at），用於計算回覆率。
REPLIED_FEEDBACK_STATUSES = {FeedbackStatus.CONFIRMED.value, FeedbackStatus.UNCERTAIN.value}


# 事件／回饋的操作標記
class OperationLabel(str, Enum):
    AC = "ac"                        # 冷氣
    DEHUMIDIFIER = "dehumidifier"    # 除濕機
    WINDOW = "window"                # 開窗／關窗
    MIXED = "mixed"                  # 同時多種操作，保留不拆成單一標籤
    NONE_CONFIRMED = "none_confirmed"  # 使用者明確表示沒有操作（A 的線上值為 none）
    OTHER = "other"                  # 使用者回覆了，但不屬於上列任何一種
    UNKNOWN = "unknown"              # 未回覆、不確定或無法連到事件


OPERATED_LABELS = {OperationLabel.AC, OperationLabel.DEHUMIDIFIER, OperationLabel.WINDOW, OperationLabel.MIXED}

# A 的線上值 → B 內部值。A 規格用 `none`，B 內部用較明確的 `none_confirmed`。
_LABEL_FROM_A = {"none": OperationLabel.NONE_CONFIRMED.value}
# B 內部值 → 寫回 A 時的值。
_LABEL_TO_A = {OperationLabel.NONE_CONFIRMED.value: "none"}


def normalize_operation_label(value: str | None) -> str:
    """A 或 B 的操作標記 → B 內部值。None／空值視為 unknown。

    未知字串會拋 ValueError：兩邊都是封閉集合，拼錯應該當場暴露，
    靜默轉成 unknown 會讓分類指標失真。
    """
    if value is None or value == "":
        return OperationLabel.UNKNOWN.value
    v = str(value)
    return OperationLabel(_LABEL_FROM_A.get(v, v)).value


def to_a_operation_label(value: str | None) -> str | None:
    """B 內部值 → 寫回 A 的值；unknown 在 A 的欄位是 null。"""
    if value is None:
        return None
    v = OperationLabel(normalize_operation_label(value)).value
    if v == OperationLabel.UNKNOWN.value:
        return None
    return _LABEL_TO_A.get(v, v)


def to_binary(label: str | OperationLabel) -> int | None:
    """有操作=1、明確無操作=0、未知或 other=None（排除，不當作負類）。

    other 代表使用者確實回覆但不屬於三種設備操作，無法判定有無操作，
    因此排除在分類之外，並在報告中單獨計數。
    """
    lab = OperationLabel(normalize_operation_label(label))
    if lab in OPERATED_LABELS:
        return 1
    if lab is OperationLabel.NONE_CONFIRMED:
        return 0
    return None


def parse_time(value: str | datetime | None) -> datetime | None:
    """解析 ISO 8601（含 A 的 UTC `...Z` 與毫秒）；沒有時區的字串視為 Asia/Taipei。"""
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
    """B 對外輸出格式：Asia/Taipei、秒精度。A 規格允許帶時區的 ISO 8601。"""
    return None if dt is None else dt.astimezone(TZ_TAIPEI).isoformat(timespec="seconds")


def fmt_utc_ms(dt: datetime | str | None) -> str | None:
    """A 的儲存格式：UTC、毫秒、以 Z 結尾（`YYYY-MM-DDTHH:mm:ss.SSSZ`）。"""
    t = parse_time(dt)
    if t is None:
        return None
    return t.astimezone(timezone.utc).isoformat(timespec="milliseconds").replace("+00:00", "Z")


def now_taipei() -> datetime:
    return datetime.now(TZ_TAIPEI)
