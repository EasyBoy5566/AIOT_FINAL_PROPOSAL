"""一鍵跑完 B 的評估流程，輸出可對回資料版本的結果與報告。

真實資料（A 第 3 週匯出）：
    python -m experiments.run_all --measurements export/measurements.json \
        --feedback export/feedback.json --op-log export/op_log.json --room room1 --tag real-w3

只測流程（合成資料，報告會標示）：
    python -m experiments.run_all --synthetic --tag dryrun

輸入檔可以是 JSON（list，或 {"rows": [...]}）或 CSV。
輸出：experiments/results/<tag>/ 下的 manifest.json、events.json、classification_*.json、ask_burden.json、report.md
"""
from __future__ import annotations

import argparse
import csv
import json
from pathlib import Path

from experiments import evaluate as ev
from src.analytics.events import EventConfig, detect_runs, merge_runs
from src.analytics.labels import LABEL_VERSION, label_counts, link_labels
from src.common.schema import parse_time
from src.knowledge.rules import RULESET_VERSION


def load_rows(path: str | None) -> tuple[list[dict], str]:
    if not path:
        return [], "none"
    p = Path(path)
    if p.suffix.lower() == ".csv":
        with p.open(encoding="utf-8-sig") as f:
            rows = list(csv.DictReader(f))
        for r in rows:
            for k in ("temperature_c", "relative_humidity_pct"):
                if k in r:
                    r[k] = float(r[k]) if r[k] not in ("", None) else None
        return rows, "real"
    data = json.loads(p.read_text(encoding="utf-8"))
    if isinstance(data, dict):
        return data.get("rows", []), data.get("data_origin", "real")
    return data, "real"


def pct(x: float | None) -> str:
    return "—" if x is None else f"{x:.3f}"


def cls_table(r: dict) -> str:
    lines = []
    if r.get("fixed_rule") is None:
        return "（沒有測試集）\n"
    for name in ("fixed_rule", "decision_tree"):
        m = r.get(name)
        if not m:
            lines.append(f"| {name} | — | — | — | 未訓練 |")
            continue
        per = "；".join(f"{k}: P={v['precision']:.2f} R={v['recall']:.2f} (n={v['support']})" for k, v in m["per_class"].items())
        lines.append(f"| {name} | {m['n']} | {m['macro_f1']:.3f} | {m['accuracy']:.3f} | {per} |")
    out = "| 方法 | 測試數 | macro-F1 | accuracy | 各類 precision／recall |\n| --- | --- | --- | --- | --- |\n" + "\n".join(lines) + "\n"
    for name in ("fixed_rule", "decision_tree"):
        m = r.get(name)
        if m:
            out += f"\n{name} 混淆矩陣（列=真實、欄=預測，順序 {m['labels']}）：`{m['confusion_matrix']}`\n"
    return out


def write_report(out: Path, manifest: dict, label_dist: dict, bin_r: dict, mc_r: dict, ask: dict) -> None:
    banner = ("> ⚠️ **本報告使用合成資料，只用來驗證流程，數值不可作為實驗結果。**\n\n"
              if manifest["data_origin"] == "synthetic" else
              "> 資料來源：本組自蒐實測資料（A 匯出）。\n\n")
    md = f"""# B 評估報告｜{manifest['tag']}

{banner}## 版本與資料

| 項目 | 值 |
| --- | --- |
| 資料來源 | {manifest['data_origin']} |
| 量測筆數／指紋 | {manifest['n_measurements']}／`{manifest['measurements_fingerprint']}` |
| 量測期間 | {manifest['period'][0]} ～ {manifest['period'][1]} |
| 監測時數 | {manifest['monitored_hours']} |
| 事件偵測 | {manifest['env']['detector_version']}，設定 {manifest['event_config']['version']} |
| 標記 | {LABEL_VERSION}；規則庫 {RULESET_VERSION}；評估 {ev.EVAL_VERSION}；固定規則 {ev.FIXED_RULE_VERSION} |
| 環境 | Python {manifest['env']['python']}、scikit-learn {manifest['env']['sklearn']} |

資料品質：{json.dumps(manifest['quality'], ensure_ascii=False)[:600]}

## 事件與標記

候選偵測 {manifest['n_runs']} 次，合併後 {manifest['n_events']} 個事件。標記分布：{label_dist}

unknown（未回覆／不確定／無紀錄）不當作「無操作」，分類時排除。

## 事件辨識：有操作／無操作

切分：依事件開始時間，較晚 {int(bin_r['split']['test_frac']*100)}% 作測試，切點 {bin_r['split']['cutoff_start_at']}。
訓練 {bin_r['split']['n_train']}（{bin_r['split']['train_class_dist']}），測試 {bin_r['split']['n_test']}（{bin_r['split']['test_class_dist']}）。排除 {bin_r['n_excluded_unknown_or_other']} 筆。

{cls_table(bin_r)}
採用：**{bin_r.get('selected', 'fixed_rule')}**

限制：{'；'.join(bin_r['limitations']) or '無'}

## 事件辨識：多類別（探索性）

訓練 {mc_r['split']['n_train']}（{mc_r['split']['train_class_dist']}），測試 {mc_r['split']['n_test']}（{mc_r['split']['test_class_dist']}）。

{cls_table(mc_r)}
限制：{'；'.join(mc_r['limitations']) or '無'}

## 詢問負擔

| 政策 | 詢問數 | 每小時詢問 | 重複詢問率 | 實際回覆率 |
| --- | --- | --- | --- | --- |
| 基礎詢問（每次偵測都問） | {ask['baseline']['n_asks']} | {pct(ask['baseline']['asks_per_hour'])} | {pct(ask['baseline']['duplicate_ask_rate'])} | {pct(ask['baseline']['reply_rate_actual'])} |
| 事件合併＋{ask['merged_cooldown']['cooldown_min']:.0f} 分鐘間隔 | {ask['merged_cooldown']['n_asks']} | {pct(ask['merged_cooldown']['asks_per_hour'])} | {pct(ask['merged_cooldown']['duplicate_ask_rate'])} | {pct(ask['merged_cooldown']['reply_rate_actual'])} |

冷卻間隔擋下 {ask['merged_cooldown']['n_suppressed_by_cooldown']} 個事件（保留為 unknown）。{ask['note']}

## 失敗案例與待辦

- [ ] 從 events.json 挑出測試集中預測錯誤的事件，附上量測曲線與回覆時間，說明原因。
- [ ] 標明哪些結果是即時實測、哪些是資料回放。
"""
    (out / "report.md").write_text(md, encoding="utf-8")


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--measurements")
    ap.add_argument("--feedback")
    ap.add_argument("--op-log")
    ap.add_argument("--room", default="room1")
    ap.add_argument("--synthetic", action="store_true")
    ap.add_argument("--days", type=int, default=14)
    ap.add_argument("--seed", type=int, default=7)
    ap.add_argument("--test-frac", type=float, default=0.3)
    ap.add_argument("--tag", default="run")
    ap.add_argument("--out-root", default="experiments/results")
    a = ap.parse_args(argv)

    cfg = EventConfig()
    if a.synthetic:
        from experiments.simulate import simulate, synth_feedback
        sim = simulate(a.days, a.seed, room_id=a.room)
        meas, op_log, origin = sim["measurements"], sim["op_log"], "synthetic"
        runs, quality = detect_runs(meas, a.room, cfg)
        events = merge_runs(runs, cfg)
        feedback = synth_feedback(events, sim["truth"], sim["reply_prob"], a.seed)
    else:
        if not a.measurements:
            ap.error("需要 --measurements 或 --synthetic")
        meas, origin = load_rows(a.measurements)
        feedback = load_rows(a.feedback)[0] if a.feedback else None
        op_log = load_rows(a.op_log)[0] if a.op_log else []
        meas = [m for m in meas if m.get("room_id", a.room) == a.room]
        runs, quality = detect_runs(meas, a.room, cfg)
        events = merge_runs(runs, cfg)

    labeled = link_labels(events, feedback, op_log)
    times = sorted(parse_time(m["measured_at"]) for m in meas)
    hours = (times[-1] - times[0]).total_seconds() / 3600 if len(times) > 1 else 0.0

    out = Path(a.out_root) / a.tag
    out.mkdir(parents=True, exist_ok=True)
    manifest = {"tag": a.tag, "data_origin": origin, "n_measurements": len(meas),
                "measurements_fingerprint": ev.data_fingerprint(meas),
                "period": [times[0].isoformat() if times else None, times[-1].isoformat() if times else None],
                "monitored_hours": round(hours, 1), "quality": quality, "n_runs": len(runs),
                "n_events": len(events), "event_config": cfg.as_dict(), "env": ev.env_info(),
                "inputs": {"measurements": a.measurements, "feedback": a.feedback, "op_log": a.op_log,
                           "synthetic_seed": a.seed if a.synthetic else None}}
    bin_r = ev.compare_classifiers(labeled, "binary", a.test_frac)
    mc_r = ev.compare_classifiers(labeled, "multiclass", a.test_frac)
    ask = ev.ask_burden(runs, hours, feedback)
    dump = lambda name, obj: (out / name).write_text(json.dumps(obj, ensure_ascii=False, indent=2), encoding="utf-8")  # noqa: E731
    dump("manifest.json", manifest)
    dump("events.json", labeled)
    dump("classification_binary.json", bin_r)
    dump("classification_multiclass.json", mc_r)
    dump("ask_burden.json", ask)
    write_report(out, manifest, label_counts(labeled), bin_r, mc_r, ask)
    print(f"[{origin}] events={len(events)} labels={label_counts(labeled)} -> {out}/report.md")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
