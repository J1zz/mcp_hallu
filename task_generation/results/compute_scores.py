"""
Compute per-model score statistics from task_results JSON files.
Outputs: scores_summary.csv and scores_summary.json
"""

import json
import os
import glob
import csv
from collections import defaultdict

RESULTS_DIR = os.path.dirname(os.path.abspath(__file__))


def load_model_records(model_dir):
    records = []
    task_results_dir = os.path.join(model_dir, "task_results")
    if not os.path.isdir(task_results_dir):
        return records
    for fpath in glob.glob(os.path.join(task_results_dir, "*.json")):
        with open(fpath) as f:
            d = json.load(f)
        score = d.get("hallu_score")
        if score is None:
            continue
        records.append(
            {
                "file": os.path.basename(fpath),
                "difficulty": d.get("DIFFICULTY"),
                "bucket": d.get("BUCKET"),
                "hallucination_type": d.get("HALLUCINATION_TYPE"),
                "score": float(score),
                "hallu_pass": d.get("hallu_pass"),
            }
        )
    return records


def stats(scores):
    if not scores:
        return {"count": 0, "mean": None, "pass_rate": None}
    return {
        "count": len(scores),
        "mean": round(sum(scores) / len(scores), 4),
    }


def compute_model_stats(records):
    result = {}

    # Overall
    all_scores = [r["score"] for r in records]
    result["overall"] = stats(all_scores)

    # By difficulty
    by_diff = defaultdict(list)
    for r in records:
        if r["difficulty"]:
            by_diff[r["difficulty"]].append(r["score"])
    result["by_difficulty"] = {d: stats(s) for d, s in sorted(by_diff.items())}

    # By bucket
    by_bucket = defaultdict(list)
    for r in records:
        if r["bucket"]:
            by_bucket[r["bucket"]].append(r["score"])
    result["by_bucket"] = {b: stats(s) for b, s in sorted(by_bucket.items())}

    # By hallucination type
    by_type = defaultdict(list)
    for r in records:
        if r["hallucination_type"]:
            by_type[r["hallucination_type"]].append(r["score"])
    result["by_type"] = {t: stats(s) for t, s in sorted(by_type.items())}

    return result


def main():
    models = sorted(
        [
            d
            for d in os.listdir(RESULTS_DIR)
            if os.path.isdir(os.path.join(RESULTS_DIR, d))
            and os.path.isdir(os.path.join(RESULTS_DIR, d, "task_results"))
        ]
    )

    all_stats = {}
    for model in models:
        model_dir = os.path.join(RESULTS_DIR, model)
        records = load_model_records(model_dir)
        if not records:
            print(f"  [SKIP] {model}: no scored records")
            continue
        all_stats[model] = compute_model_stats(records)
        n = all_stats[model]["overall"]["count"]
        mean = all_stats[model]["overall"]["mean"]
        print(f"  {model}: n={n}, overall_mean={mean}")

    # Save JSON
    json_out = os.path.join(RESULTS_DIR, "scores_summary.json")
    with open(json_out, "w") as f:
        json.dump(all_stats, f, indent=2, ensure_ascii=False)
    print(f"\nSaved: {json_out}")

    # Save CSV (one row per model × difficulty)
    csv_out = os.path.join(RESULTS_DIR, "scores_summary.csv")
    rows = []
    for model, mstats in all_stats.items():
        # Overall row
        rows.append(
            {
                "model": model,
                "split": "overall",
                "split_value": "all",
                "count": mstats["overall"]["count"],
                "mean_score": mstats["overall"]["mean"],
            }
        )
        for diff, s in mstats["by_difficulty"].items():
            rows.append(
                {
                    "model": model,
                    "split": "difficulty",
                    "split_value": diff,
                    "count": s["count"],
                    "mean_score": s["mean"],
                }
            )
        for bucket, s in mstats["by_bucket"].items():
            rows.append(
                {
                    "model": model,
                    "split": "bucket",
                    "split_value": bucket,
                    "count": s["count"],
                    "mean_score": s["mean"],
                }
            )
        for htype, s in mstats["by_type"].items():
            rows.append(
                {
                    "model": model,
                    "split": "hallucination_type",
                    "split_value": htype,
                    "count": s["count"],
                    "mean_score": s["mean"],
                }
            )

    with open(csv_out, "w", newline="") as f:
        writer = csv.DictWriter(
            f, fieldnames=["model", "split", "split_value", "count", "mean_score"]
        )
        writer.writeheader()
        writer.writerows(rows)
    print(f"Saved: {csv_out}")


if __name__ == "__main__":
    main()
