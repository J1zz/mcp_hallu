"""Agent 轨迹解析工具：从 CSV 行中提取工具调用列表和模型回复。"""

import json
import math
import logging
from typing import Any, Dict, List, Optional

logger = logging.getLogger(__name__)


def _safe_str(val: Any) -> str:
    """将 pandas NaN / None / 任意值转为字符串；NaN → ""。"""
    if val is None:
        return ""
    try:
        if not isinstance(val, str) and math.isnan(float(val)):
            return ""
    except (TypeError, ValueError):
        pass
    return str(val)


def parse_tool_calls_from_trajectory(trajectory_str: Optional[str]) -> List[str]:
    """从 AgentOutput JSON 字符串提取工具调用名称列表（顺序保留）。

    支持两种格式：
      - AgentOutput: [{type:'message', data:{tool_calls:[{function:{name:...}}]}}]
      - 简化格式:    [{tool_name:..., parameters:..., response:...}]
    """
    s = _safe_str(trajectory_str)
    if not s:
        return []
    try:
        data = json.loads(s)
    except (json.JSONDecodeError, TypeError):
        return []
    if not isinstance(data, list):
        return []

    names = []
    for item in data:
        if not isinstance(item, dict):
            continue
        if item.get("type") == "message":
            for tc in (item.get("data", {}).get("tool_calls") or []):
                name = tc.get("function", {}).get("name", "")
                if name:
                    names.append(name)
        elif "tool_name" in item and item["tool_name"]:
            names.append(item["tool_name"])
    return names


def parse_model_response(row: Dict[str, Any]) -> str:
    """从 CSV 行中提取模型最终文字回复。"""
    for col in ("script_model_response", "response", "model_response"):
        val = _safe_str(row.get(col))
        if val:
            return val
    return ""
