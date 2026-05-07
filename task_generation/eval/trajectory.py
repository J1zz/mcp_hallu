"""Agent trajectory parsing: extract tool calls and model response from a CSV row."""

import json
import math
import logging
from typing import Any, Dict, List, Optional

logger = logging.getLogger(__name__)


def _safe_str(val: Any) -> str:
    """Convert pandas NaN / None / any value to str; NaN → empty string."""
    if val is None:
        return ""
    try:
        if not isinstance(val, str) and math.isnan(float(val)):
            return ""
    except (TypeError, ValueError):
        pass
    return str(val)


def parse_tool_calls_from_trajectory(trajectory_str: Optional[str]) -> List[str]:
    """Extract ordered tool call name list from a trajectory JSON string.

    Supports two formats:
      - AgentOutput: [{type:'message', data:{tool_calls:[{function:{name:...}}]}}]
      - Simplified:  [{tool_name:..., parameters:..., response:...}]
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


def parse_full_trajectory(trajectory_str: Optional[str]) -> List[Dict[str, Any]]:
    """Extract full execution records (with parameters and responses) from a trajectory JSON string.

    Each item in the returned list has the shape:
      {"tool_name": str, "parameters": dict, "response": any}

    Supports the same two formats as parse_tool_calls_from_trajectory.
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

    steps = []
    for item in data:
        if not isinstance(item, dict):
            continue
        if item.get("type") == "message":
            for tc in (item.get("data", {}).get("tool_calls") or []):
                name = tc.get("function", {}).get("name", "")
                if not name:
                    continue
                try:
                    params = json.loads(tc.get("function", {}).get("arguments") or "{}")
                except (json.JSONDecodeError, TypeError):
                    params = {}
                steps.append({"tool_name": name, "parameters": params, "response": None})
        elif "tool_name" in item and item["tool_name"]:
            steps.append({
                "tool_name":  item["tool_name"],
                "parameters": item.get("parameters") or {},
                "response":   item.get("response"),
            })
    return steps


def parse_full_trajectory_from_conversation(conversation_str: Optional[str]) -> List[Dict[str, Any]]:
    """Extract full execution records from an OpenAI-format conversation history.

    Matches each assistant tool_call to its tool response via tool_call_id.
    Returns [{"tool_name": str, "parameters": dict, "response": any}].
    """
    s = _safe_str(conversation_str)
    if not s:
        return []
    try:
        messages = json.loads(s)
    except (json.JSONDecodeError, TypeError):
        return []
    if not isinstance(messages, list):
        return []

    # Map tool_call_id -> parsed response content
    tool_responses: Dict[str, Any] = {}
    for msg in messages:
        if not isinstance(msg, dict) or msg.get("role") != "tool":
            continue
        call_id = msg.get("tool_call_id")
        content = msg.get("content")
        if call_id is not None:
            if isinstance(content, str):
                try:
                    content = json.loads(content)
                except (json.JSONDecodeError, TypeError):
                    pass
            tool_responses[call_id] = content

    # Walk assistant messages in order, preserving call order
    steps = []
    for msg in messages:
        if not isinstance(msg, dict) or msg.get("role") != "assistant":
            continue
        for tc in (msg.get("tool_calls") or []):
            if not isinstance(tc, dict):
                continue
            call_id = tc.get("id")
            fn      = tc.get("function", {})
            name    = fn.get("name", "")
            if not name:
                continue
            try:
                params = json.loads(fn.get("arguments") or "{}")
            except (json.JSONDecodeError, TypeError):
                params = {}
            steps.append({
                "tool_name":  name,
                "parameters": params,
                "response":   tool_responses.get(call_id),
            })
    return steps


def parse_model_response(row: Dict[str, Any]) -> str:
    """Extract the model's final text response from a CSV row."""
    for col in ("script_model_response", "response", "model_response"):
        val = _safe_str(row.get(col))
        if val:
            return val
    return ""
