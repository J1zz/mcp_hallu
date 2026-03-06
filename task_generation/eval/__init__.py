"""eval — 幻觉评测子包。

模块分工：
  config.py     路径、环境变量、可选依赖初始化
  schema.py     HallucinationType、Task、STATEFUL_BUCKETS
  data_io.py    JSONL/CSV 加载与转换
  trajectory.py Agent 轨迹解析
  scoring.py    四种评分策略 + route_and_score 路由器
  runner.py     评分主循环、报告输出、完整 pipeline
"""

from .schema import HallucinationType, Task, STATEFUL_BUCKETS
from .data_io import load_tasks_from_jsonl, tasks_to_csv, build_tasks_from_completion_csv
from .trajectory import parse_tool_calls_from_trajectory, parse_model_response
from .scoring import (
    score_confusion_trap,
    score_void_trap,
    score_parallel_execution,
    score_state_assertions,
    route_and_score,
)
from .runner import evaluate_from_completion_csv, run_full_pipeline, print_eval_report

__all__ = [
    "HallucinationType", "Task", "STATEFUL_BUCKETS",
    "load_tasks_from_jsonl", "tasks_to_csv", "build_tasks_from_completion_csv",
    "parse_tool_calls_from_trajectory", "parse_model_response",
    "score_confusion_trap", "score_void_trap",
    "score_parallel_execution", "score_state_assertions", "route_and_score",
    "evaluate_from_completion_csv", "run_full_pipeline", "print_eval_report",
]
