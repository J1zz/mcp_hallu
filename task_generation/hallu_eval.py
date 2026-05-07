"""hallu_eval.py

Full evaluation pipeline (no need to pre-run GT):

  uv run hallu-eval --input final_tasks/memory_tasks.jsonl \
                    --model gpt-5-chat-2025-08-07 \
                    --docker-snapshot

Pipeline overview
-----------------
1. Load JSONL tasks (supports Confusion / Void / Memory / Reasoning four hallucination types)
2. Start the model-under-test Agent, execute all tasks concurrently (--concurrency controls parallelism)
3. After each task Agent completes, immediately:
   a. If the task type is Memory/Reasoning Trap and strategy=dynamic_script,
      auto-run the GT reference script (run_one_gt_script) to generate gt_execution_log
      (uses the same eval model config as LLM-as-judge)
   b. Call route_and_score to score the task
   c. Write per-task JSON result file (default: results/task_results/<task_id>.json)
   d. Append to the summary CSV
4. Print a summary report after all tasks complete

Skip agent execution and score an existing completion CSV directly:

  uv run hallu-eval --from-completion-csv results/confusion_eval_completion.csv \\
                    --input new_task/confusion_tasks.jsonl \\
                    --output results/confusion_eval.csv

Convert JSONL to an intermediate CSV only (for debugging):

  uv run hallu-eval --input new_task/confusion_tasks.jsonl \\
                    --convert-only \\
                    --output /tmp/tasks.csv
"""

import argparse
import asyncio
import logging
import os
import re
import sys
from pathlib import Path
from typing import Optional

from eval import (
    HallucinationType,
    evaluate_from_completion_csv,
    load_tasks_from_jsonl,
    run_full_pipeline,
    tasks_to_csv,
)

logger = logging.getLogger(__name__)

RESULTS_DIR = Path(__file__).parent / "results"


def _model_slug(model: str) -> str:
    """Convert a model identifier to a filesystem-safe directory name.

    Examples:
      "openai/gpt-4o"                  → "openai_gpt-4o"
      "anthropic.claude-sonnet-4-6"    → "anthropic.claude-sonnet-4-6"
    """
    return re.sub(r"[/\\:*?\"<>|]", "_", model).strip("_")


def _default_output(model: str, input_path: Optional[str]) -> str:
    """Derive a default output CSV path under results/<model_slug>/."""
    slug    = _model_slug(model)
    stem    = Path(input_path).stem if input_path else "eval"
    out_dir = RESULTS_DIR / slug
    out_dir.mkdir(parents=True, exist_ok=True)
    return str(out_dir / f"{stem}.csv")


def _parse_rows(value: str) -> list[int]:
    """Parse a row-selection string into a sorted list of 0-based indices.

    Accepted formats (can be mixed with commas):
      "5"        → [5]
      "0,2,5"    → [0, 2, 5]
      "0-4"      → [0, 1, 2, 3, 4]
      "0,2-5,8"  → [0, 2, 3, 4, 5, 8]
    """
    indices: set[int] = set()
    for part in value.split(","):
        part = part.strip()
        if "-" in part:
            lo, hi = part.split("-", 1)
            indices.update(range(int(lo), int(hi) + 1))
        else:
            indices.add(int(part))
    return sorted(indices)


def _apply_row_filter(tasks: list, args) -> list:
    """Apply --rows and/or --num-tasks filtering to a task list."""
    if args.rows is not None:
        tasks = [tasks[i] for i in args.rows if i < len(tasks)]
    if args.num_tasks:
        tasks = tasks[:args.num_tasks]
    return tasks


def _parse_args():
    p = argparse.ArgumentParser(
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=__doc__,
    )
    src = p.add_mutually_exclusive_group()
    src.add_argument("--input", help="JSONL task file (full pipeline or combined with --from-completion-csv)")
    src.add_argument("--from-completion-csv", dest="completion_csv", help="Score an existing completion CSV directly (skip agent execution)")
    p.add_argument("--convert-only", action="store_true", help="Convert JSONL to intermediate CSV only, no agent execution or scoring (requires --input)")
    p.add_argument("--output", default=None,
                   help="Output CSV path (default: results/<model>/<input_stem>.csv)")
    p.add_argument("--model", default=os.getenv("LLM_MODEL", "openai/gpt-4o"))
    p.add_argument("--server-url", default=os.getenv("SERVER_URL", "http://localhost:3001"), help="mcp-atlas agent server URL")
    p.add_argument("--concurrency", type=int, default=3, help="number of concurrent agent requests")
    p.add_argument("--num-tasks", type=int, default=None, help="limit task count (for debugging)")
    p.add_argument(
        "--rows",
        type=_parse_rows,
        default=None,
        metavar="ROWS",
        help="Specify task row indices to run (0-based): single '5', comma list '0,2,5', range '0-4', mixed '0,2-5,8'",
    )
    p.add_argument("--pass-threshold", type=float, default=0.8, help="pass score threshold [0.0-1.0]")
    p.add_argument(
        "--json-output-dir",
        default=None,
        help="Per-task JSON result output directory (default: task_results/ under the output CSV directory)",
    )
    # --input can be combined with --from-completion-csv (to supply GT scripts for dynamic_script tasks)
    # When --from-completion-csv is used alone, --input is optional
    p.add_argument(
        "--jsonl-for-gt",
        default=None,
        dest="jsonl_for_gt",
        help="Used with --from-completion-csv: specify original JSONL file to provide GT scripts for dynamic_script tasks (defaults to --input)",
    )
    p.add_argument(
        "--docker-snapshot",
        action="store_true",
        dest="docker_snapshot",
        help=(
            "Enable Docker /data snapshot isolation mode: automatically restore the container /data to its "
            "initial state before each task, completely eliminating inter-task state contamination. "
            "In this mode concurrency is automatically reduced to 1 (serial execution). "
            "Each snapshot/restore takes ~1 s; total overhead for 476 tasks is ~8 minutes."
        ),
    )
    return p.parse_args()


def main():
    args = _parse_args()

    # Resolve output path: explicit > auto-derived from model + input stem
    output = args.output or _default_output(args.model, args.input)

    # Convert-only: JSONL → intermediate CSV, no agent execution or scoring
    if args.convert_only:
        if not args.input:
            logger.error("--convert-only requires --input")
            sys.exit(1)
        tasks = load_tasks_from_jsonl(args.input)
        tasks = _apply_row_filter(tasks, args)
        tasks_to_csv(tasks, output)
        print(f"\nConverted: {output}")
        return

    # Score an existing completion CSV (skip agent execution)
    if args.completion_csv:
        jsonl_path = args.jsonl_for_gt or args.input or None
        evaluate_from_completion_csv(
            completion_csv_path=args.completion_csv,
            output_csv_path=output,
            pass_threshold=args.pass_threshold,
            jsonl_path=jsonl_path,
            json_output_dir=args.json_output_dir,
        )
        return

    # Full pipeline: agent execution + GT generation + scoring
    if not args.input:
        logger.error("Specify --input or --from-completion-csv")
        sys.exit(1)

    all_tasks = load_tasks_from_jsonl(args.input)
    all_tasks = _apply_row_filter(all_tasks, args)

    dynamic_script_tasks = [
        t for t in all_tasks
        if t.hallucination_type in (HallucinationType.MEMORY, HallucinationType.REASONING)
        and t.ground_truth.get("strategy") == "dynamic_script"
        and not t.gt_execution_ok
    ]
    if dynamic_script_tasks:
        print(
            f"\n{len(dynamic_script_tasks)} dynamic_script tasks will have GT logs "
            f"generated automatically after agent execution.\n"
        )

    print(f"\nResults will be saved to: {output}\n")

    asyncio.run(run_full_pipeline(
        jsonl_path=args.input,
        model=args.model,
        output_csv=output,
        server_url=args.server_url,
        concurrency=args.concurrency,
        num_tasks=args.num_tasks,
        task_indices=args.rows,
        pass_threshold=args.pass_threshold,
        json_output_dir=args.json_output_dir,
        use_docker_snapshot=args.docker_snapshot,
    ))


if __name__ == "__main__":
    main()
