"""Data loading and conversion: JSONL → Task, Task → CSV, completion CSV → Task."""

import json
import logging
from pathlib import Path
from typing import Any, List

import pandas as pd

from .schema import Task
from .trajectory import _safe_str

logger = logging.getLogger(__name__)


def _parse_json_col(val: Any, default: Any) -> Any:
    """Safely parse a JSON string from a CSV cell; returns default on failure."""
    if not val:
        return default
    try:
        return json.loads(val)
    except Exception:
        return default


def load_tasks_from_jsonl(jsonl_path: str) -> List[Task]:
    """Load a list of Task objects from a task JSONL file."""
    path = Path(jsonl_path)
    if not path.exists():
        raise FileNotFoundError(f"JSONL file not found: {jsonl_path}")

    tasks = []
    with open(path, encoding="utf-8") as f:
        for idx, line in enumerate(f):
            line = line.strip()
            if not line:
                continue
            try:
                obj = json.loads(line)
            except json.JSONDecodeError as e:
                logger.warning(f"Failed to parse line {idx+1}, skipping: {e}")
                continue
            tasks.append(Task(
                task_id=obj.get("task_id") or f"{path.stem}_{idx}",
                bucket=obj.get("bucket", "UNKNOWN"),
                hallucination_type=obj.get("hallucination_type", ""),
                difficulty=obj.get("difficulty", ""),
                prompt=obj.get("task", ""),
                available_tools=obj.get("available_tools", []),
                ground_truth=obj.get("ground_truth", {}),
                evaluation_rules=obj.get("evaluation_rules", {}),
                claims=obj.get("claims", []),
                should_stop_early=obj.get("should_stop_early", False),
                gt_execution_log=obj.get("gt_execution_log"),
                gt_execution_ok=bool(obj.get("gt_execution_ok", False)),
            ))

    logger.info(f"Loaded {len(tasks)} tasks from {jsonl_path}")
    return tasks


def tasks_to_csv(tasks: List[Task], output_path: str) -> pd.DataFrame:
    """Write a list of Tasks to a CSV that can be directly read by mcp_completion_script.py."""
    rows = []
    for t in tasks:
        # Preserve the full claims structure (including required_tool, branch, dependency_on_step, etc.)
        # rather than downgrading to a plain string list, otherwise dependency-order and branch
        # validation will break due to missing fields.
        claims_serializable = [
            c if isinstance(c, dict) else {"description": c}
            for c in t.claims
        ]
        rows.append({
            "TASK":               t.task_id,
            "ENABLED_TOOLS":      json.dumps(t.available_tools, ensure_ascii=False),
            "PROMPT":             t.prompt,
            "GT_EXECUTION_LOG": t.gt_execution_log or "",
            "GTFA_CLAIMS":        json.dumps(claims_serializable, ensure_ascii=False),
            "HALLUCINATION_TYPE": t.hallucination_type,
            "BUCKET":             t.bucket,
            "DIFFICULTY":         t.difficulty,
            "SHOULD_STOP_EARLY":  str(t.should_stop_early),
            "EVALUATION_RULES":   json.dumps(t.evaluation_rules, ensure_ascii=False),
            "STATE_ASSERTIONS":   json.dumps(t.ground_truth.get("state_assertions", []), ensure_ascii=False),
            "GT_STRATEGY":        t.ground_truth.get("strategy", ""),
        })

    df  = pd.DataFrame(rows)
    out = Path(output_path)
    out.parent.mkdir(parents=True, exist_ok=True)
    df.to_csv(out, index=False)
    logger.info(f"Wrote {len(rows)} tasks to CSV: {out}")
    return df


def build_tasks_from_completion_csv(df: pd.DataFrame) -> List[Task]:
    """Rebuild a list of Task objects from a completion CSV (used for scoring only)."""
    tasks = []
    for idx, row in df.iterrows():
        assertions   = _parse_json_col(row.get("STATE_ASSERTIONS"), [])
        claims_raw   = _parse_json_col(row.get("GTFA_CLAIMS"), [])
        claims       = [{"description": c} if isinstance(c, str) else c for c in claims_raw]
        gt_log       = _safe_str(row.get("GT_EXECUTION_LOG") or row.get("TRAJECTORY", ""))
        gt_strategy  = _safe_str(row.get("GT_STRATEGY", ""))

        tasks.append(Task(
            task_id=str(row.get("TASK", f"row_{idx}")),
            bucket=str(row.get("BUCKET", "ANALYTICS") or "ANALYTICS"),
            hallucination_type=str(row.get("HALLUCINATION_TYPE", "Memory Trap") or "Memory Trap"),
            difficulty=str(row.get("DIFFICULTY", "") or ""),
            prompt=str(row.get("PROMPT", "")),
            available_tools=_parse_json_col(row.get("ENABLED_TOOLS"), []),
            ground_truth={"state_assertions": assertions, "strategy": gt_strategy},
            evaluation_rules=_parse_json_col(row.get("EVALUATION_RULES"), {}),
            claims=claims,
            should_stop_early=str(row.get("SHOULD_STOP_EARLY", "False")).lower() == "true",
            gt_execution_log=gt_log,
            gt_execution_ok=bool(gt_log),
        ))
    return tasks
