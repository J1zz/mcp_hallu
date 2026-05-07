"""eval — hallucination evaluation sub-package.

Module responsibilities:
  config.py     paths, environment variables, optional dependency initialisation
  schema.py     HallucinationType, Task, STATEFUL_BUCKETS
  data_io.py    JSONL/CSV loading and conversion
  trajectory.py agent trajectory parsing
  scoring.py    four scoring strategies + route_and_score router
  runner.py     scoring main loop, report output, full pipeline
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
