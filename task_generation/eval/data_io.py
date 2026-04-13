"""数据加载与转换：JSONL → Task，Task → CSV，completion CSV → Task。"""

import json
import logging
from pathlib import Path
from typing import Any, List

import pandas as pd

from .schema import Task
from .trajectory import _safe_str

logger = logging.getLogger(__name__)


def _parse_json_col(val: Any, default: Any) -> Any:
    """安全解析 CSV 单元格中的 JSON 字符串；失败返回 default。"""
    if not val:
        return default
    try:
        return json.loads(val)
    except Exception:
        return default


def load_tasks_from_jsonl(jsonl_path: str) -> List[Task]:
    """从 task JSONL 文件加载 Task 列表。"""
    path = Path(jsonl_path)
    if not path.exists():
        raise FileNotFoundError(f"JSONL 文件不存在: {jsonl_path}")

    tasks = []
    with open(path, encoding="utf-8") as f:
        for idx, line in enumerate(f):
            line = line.strip()
            if not line:
                continue
            try:
                obj = json.loads(line)
            except json.JSONDecodeError as e:
                logger.warning(f"第 {idx+1} 行解析失败，跳过: {e}")
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

    logger.info(f"加载了 {len(tasks)} 条任务（{jsonl_path}）")
    return tasks


def tasks_to_csv(tasks: List[Task], output_path: str) -> pd.DataFrame:
    """将 Task 列表写成 mcp_completion_script.py 可直接读取的 CSV。"""
    rows = []
    for t in tasks:
        # 保留完整 claims 结构（含 required_tool、branch、dependency_on_step 等字段），
        # 而不是降级为纯字符串列表，否则依赖顺序验证和分支验证会因字段缺失而失效
        claims_serializable = [
            c if isinstance(c, dict) else {"description": c}
            for c in t.claims
        ]
        rows.append({
            "TASK":               t.task_id,
            "ENABLED_TOOLS":      json.dumps(t.available_tools, ensure_ascii=False),
            "PROMPT":             t.prompt,
            "TRAJECTORY":         t.gt_execution_log or "",
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
    logger.info(f"写入 {len(rows)} 条任务至 CSV: {out}")
    return df


def build_tasks_from_completion_csv(df: pd.DataFrame) -> List[Task]:
    """从 completion CSV 重建 Task 对象列表（仅用于评分）。"""
    tasks = []
    for idx, row in df.iterrows():
        assertions   = _parse_json_col(row.get("STATE_ASSERTIONS"), [])
        claims_raw   = _parse_json_col(row.get("GTFA_CLAIMS"), [])
        claims       = [{"description": c} if isinstance(c, str) else c for c in claims_raw]
        gt_log       = _safe_str(row.get("TRAJECTORY", ""))
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
