"""第 3 週評估：事件辨識（固定規則 vs 簡單決策樹）與詢問負擔（基礎詢問 vs 合併＋冷卻）。

負責人：鍾易憲（工作流 B）

原則
- 以完整事件為單位、依 start_at 時間切分，較晚的事件作測試；測試集用過後不再調參。
- 特徵只用 decision_at 以前可取得的值（events.py 的 features；summary 不使用）。
- unknown 不當負類，直接排除並報告排除數。
- 樣本不足時不訓練決策樹，記錄限制並維持規則版。
"""
from __future__ import annotations

import hashlib
import json
import platform
from dataclasses import asdict, dataclass
from datetime import datetime

from src.analytics.ask_policy import POLICY_VERSION, AskConfig, replay_baseline, replay_merged_cooldown
from src.analytics.events import DETECTOR_VERSION, EventConfig, Run
from src.common.schema import OperationLabel, parse_time, to_binary

EVAL_VERSION = "eval/1.0.0"
FIXED_RULE_VERSION = "fixed-rule/1.0.0"

NUMERIC_FEATURES = [
    "delta_temperature_c", "delta_relative_humidity_pct",
    "slope_temperature_c_per_min", "slope_relative_humidity_pct_per_min",
    "start_temperature_c", "start_relative_humidity_pct", "delta_dew_point_c",
    "minutes_to_decision", "hour_of_day",
]
MULTICLASS = ["ac", "dehumidifier", "window", "mixed", "none_confirmed"]


@dataclass(frozen=True)
class SampleRule:
    min_train: int = 20
    min_train_per_class: int = 5
    min_test: int = 10
    min_test_per_class: int = 3


# ---------------------------------------------------------------------------
# 固定規則基準（門檻在看資料前訂定，不用測試集調整）
# ---------------------------------------------------------------------------

def fixed_rule_binary(f: dict) -> int:
    """設備操作通常讓溫度或水氣下降；自然活動（煮飯、洗澡、日照）多讓兩者上升。"""
    return int(f["delta_temperature_c"] <= -1.0 or f["delta_dew_point_c"] <= -1.5)


def fixed_rule_multiclass(f: dict) -> str:
    dT, dH, dTd = f["delta_temperature_c"], f["delta_relative_humidity_pct"], f["delta_dew_point_c"]
    if dT <= -1.0 and dTd <= -1.5 and dH <= -3:
        return "ac" if dT <= -1.5 else "mixed"
    if dT <= -1.0:
        return "ac"
    if dTd <= -1.5:
        return "dehumidifier"
    if dT < 0 and dH > 0:
        return "window"
    return "none_confirmed"


# ---------------------------------------------------------------------------
# 切分與指標
# ---------------------------------------------------------------------------

def time_split(events: list[dict], test_frac: float = 0.3) -> tuple[list[dict], list[dict], str | None]:
    """依事件開始時間排序，最後 test_frac 為測試；回傳切點。"""
    ev = sorted(events, key=lambda e: e["start_at"])
    if len(ev) < 2:
        return ev, [], None
    k = max(1, int(round(len(ev) * (1 - test_frac))))
    k = min(k, len(ev) - 1)
    cutoff = ev[k]["start_at"]
    return ev[:k], ev[k:], cutoff


def metrics(y_true: list, y_pred: list, labels: list) -> dict:
    cm = [[sum(1 for t, p in zip(y_true, y_pred) if t == a and p == b) for b in labels] for a in labels]
    per = {}
    for i, lab in enumerate(labels):
        tp = cm[i][i]
        fp = sum(cm[r][i] for r in range(len(labels))) - tp
        fn = sum(cm[i]) - tp
        p = tp / (tp + fp) if tp + fp else 0.0
        r = tp / (tp + fn) if tp + fn else 0.0
        f1 = 2 * p * r / (p + r) if p + r else 0.0
        per[str(lab)] = {"precision": round(p, 3), "recall": round(r, 3), "f1": round(f1, 3), "support": sum(cm[i])}
    present = [k for k, v in per.items() if v["support"] > 0]
    macro = sum(per[k]["f1"] for k in present) / len(present) if present else 0.0
    acc = sum(1 for t, p in zip(y_true, y_pred) if t == p) / len(y_true) if y_true else 0.0
    return {"labels": [str(x) for x in labels], "per_class": per, "macro_f1": round(macro, 3),
            "accuracy": round(acc, 3), "confusion_matrix": cm, "n": len(y_true),
            "note": "confusion_matrix 列=真實、欄=預測；macro-F1 只平均測試集中有樣本的類別"}


def _dist(ys: list) -> dict:
    d: dict = {}
    for y in ys:
        d[str(y)] = d.get(str(y), 0) + 1
    return dict(sorted(d.items()))


def _enough(ys_train: list, ys_test: list, classes: list, sr: SampleRule) -> list[str]:
    why = []
    if len(ys_train) < sr.min_train:
        why.append(f"訓練樣本 {len(ys_train)} < {sr.min_train}")
    if len(ys_test) < sr.min_test:
        why.append(f"測試樣本 {len(ys_test)} < {sr.min_test}")
    for c in classes:
        if ys_train.count(c) < sr.min_train_per_class:
            why.append(f"訓練集類別 {c} 只有 {ys_train.count(c)} 筆")
        if ys_test.count(c) < sr.min_test_per_class:
            why.append(f"測試集類別 {c} 只有 {ys_test.count(c)} 筆")
    return why


def compare_classifiers(events: list[dict], task: str = "binary", test_frac: float = 0.3,
                        sr: SampleRule = SampleRule(), max_depth: int = 3, seed: int = 0) -> dict:
    """task = binary（有操作／無操作）或 multiclass。"""
    if task == "binary":
        labeled = [e for e in events if to_binary(e.get("label", "unknown")) is not None]
        y_of = lambda e: to_binary(e["label"])  # noqa: E731
        classes = [0, 1]
        rule = lambda f: fixed_rule_binary(f)  # noqa: E731
    else:
        labeled = [e for e in events if e.get("label") in MULTICLASS]
        y_of = lambda e: e["label"]  # noqa: E731
        classes = MULTICLASS
        rule = fixed_rule_multiclass
    excluded = len(events) - len(labeled)
    train, test, cutoff = time_split(labeled, test_frac)
    ytr, yte = [y_of(e) for e in train], [y_of(e) for e in test]
    present = [c for c in classes if c in ytr or c in yte]
    res = {
        "task": task, "eval_version": EVAL_VERSION, "fixed_rule_version": FIXED_RULE_VERSION,
        "n_events": len(events), "n_labeled": len(labeled), "n_excluded_unknown_or_other": excluded,
        "split": {"method": "time_by_event_start", "test_frac": test_frac, "cutoff_start_at": cutoff,
                  "train_period": [train[0]["start_at"], train[-1]["start_at"]] if train else None,
                  "test_period": [test[0]["start_at"], test[-1]["start_at"]] if test else None,
                  "n_train": len(train), "n_test": len(test),
                  "train_class_dist": _dist(ytr), "test_class_dist": _dist(yte)},
        "features": NUMERIC_FEATURES, "limitations": [],
    }
    if not test:
        res["limitations"].append("沒有足夠事件可切出測試集")
        return res
    res["fixed_rule"] = metrics(yte, [rule(e["features"]) for e in test], present)
    why = _enough(ytr, yte, [c for c in present], sr)
    if why:
        res["decision_tree"] = None
        res["limitations"].append("樣本不足，不訓練決策樹，維持規則版：" + "；".join(why))
        res["selected"] = "fixed_rule"
        return res
    from sklearn.tree import DecisionTreeClassifier, export_text
    X = lambda es: [[e["features"][k] for k in NUMERIC_FEATURES] for e in es]  # noqa: E731
    params = {"max_depth": max_depth, "min_samples_leaf": 3, "class_weight": "balanced", "random_state": seed}
    clf = DecisionTreeClassifier(**params).fit(X(train), ytr)
    pred = list(clf.predict(X(test)))
    pred = [int(p) if task == "binary" else str(p) for p in pred]
    res["decision_tree"] = metrics(yte, pred, present)
    res["decision_tree"]["params"] = params
    res["decision_tree"]["tree_text"] = export_text(clf, feature_names=NUMERIC_FEATURES)
    better = res["decision_tree"]["macro_f1"] > res["fixed_rule"]["macro_f1"]
    res["selected"] = "decision_tree" if better else "fixed_rule"
    res["limitations"].append("僅一次時間切分，測試樣本少時差異可能來自隨機變動；不宣稱模型普遍優於規則。")
    return res


# ---------------------------------------------------------------------------
# 詢問負擔
# ---------------------------------------------------------------------------

def ask_burden(runs: list[Run], monitored_hours: float, feedback: list[dict] | None = None,
               ask_cfg: AskConfig = AskConfig(), ev_cfg: EventConfig = EventConfig()) -> dict:
    """同一批候選偵測回放兩種詢問政策。回覆率只用實際回覆（feedback 中有 replied_at 的 event_id）。"""
    base = replay_baseline(runs)
    merged, suppressed = replay_merged_cooldown(runs, ask_cfg, ev_cfg)
    replied = {f["event_id"] for f in (feedback or []) if f.get("replied_at")}

    def summary(asks: list[dict]) -> dict:
        per_event: dict[str, int] = {}
        for a in asks:
            per_event[a["event_id"]] = per_event.get(a["event_id"], 0) + 1
        dup = sum(c - 1 for c in per_event.values())
        asked_events = set(per_event)
        return {"n_asks": len(asks), "n_events_asked": len(asked_events),
                "asks_per_hour": round(len(asks) / monitored_hours, 3) if monitored_hours else None,
                "duplicate_ask_rate": round(dup / len(asks), 3) if asks else 0.0,
                "reply_rate_actual": (round(len(asked_events & replied) / len(asked_events), 3)
                                      if feedback is not None and asked_events else None)}

    return {"policy_version": POLICY_VERSION, "monitored_hours": round(monitored_hours, 1),
            "n_runs": len(runs), "baseline": summary(base),
            "merged_cooldown": {**summary(merged), "n_suppressed_by_cooldown": len(suppressed),
                                "cooldown_min": ask_cfg.cooldown_min, "merge_gap_min": ev_cfg.merge_gap_min},
            "note": ("回覆率依實際回覆計算；實際只部署一種政策時，另一種政策的回覆率無法觀察，記為 null 或只對共同事件計算。"
                     " duplicate_ask_rate = 同一合併事件的第 2 次以後詢問 / 總詢問數。")}


def data_fingerprint(rows: list[dict]) -> str:
    h = hashlib.sha256(json.dumps(rows, sort_keys=True, ensure_ascii=False).encode()).hexdigest()
    return h[:16]


def env_info() -> dict:
    try:
        import sklearn
        skv = sklearn.__version__
    except ImportError:
        skv = None
    return {"python": platform.python_version(), "sklearn": skv, "detector_version": DETECTOR_VERSION,
            "generated_at": datetime.now().astimezone().isoformat(timespec="seconds")}


__all__ = ["compare_classifiers", "ask_burden", "time_split", "metrics", "fixed_rule_binary",
           "fixed_rule_multiclass", "data_fingerprint", "env_info", "SampleRule", "OperationLabel",
           "asdict", "parse_time"]
