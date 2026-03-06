"""数据结构：HallucinationType、Task、STATEFUL_BUCKETS。"""

from dataclasses import dataclass
from enum import Enum
from typing import Any, Dict, List, Optional


class HallucinationType(str, Enum):
    CONFUSION = "Confusion Trap"
    VOID      = "Void Trap"
    MEMORY    = "Memory Trap"
    REASONING = "Reasoning Trap"


STATEFUL_BUCKETS = {"PRODUCTIVITY", "CODING"}


@dataclass
class Task:
    task_id:            str
    bucket:             str
    hallucination_type: str
    difficulty:         str
    prompt:             str
    available_tools:    List[str]
    ground_truth:       Dict[str, Any]
    evaluation_rules:   Dict[str, Any]
    claims:             List[Dict[str, Any]]
    should_stop_early:  bool = False
    gt_execution_log:   Optional[str] = None
    gt_execution_ok:    bool = False
