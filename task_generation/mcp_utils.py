"""mcp_utils.py — 共享的 MCP 工具调用底层逻辑

从 run_gt_execution.py 抽出，供 GT 执行（run_gt_execution.py）
和评分（eval/scoring.py）共同使用，避免重复维护。
"""

import json
import os
import re
from typing import Any

import requests

MCP_SERVER_URL = os.getenv("MCP_SERVER_URL", "http://localhost:1984")


# ──────────────────────────────────────────────────────────────────────────────
# 工具返回值解析
# ──────────────────────────────────────────────────────────────────────────────

def _parse_tool_response(raw: Any) -> Any:
    """将工具返回的任意格式统一适配为 Python 对象（dict/list）或原始字符串。"""
    if isinstance(raw, (dict, list)):
        return raw
    if not isinstance(raw, str):
        return {"value": raw, "_raw_text": False}
    text = raw.strip()
    if text and text[0] in ('{', '[', '"', 't', 'f', 'n') or (text and text[0].isdigit()):
        try:
            return json.loads(text)
        except json.JSONDecodeError:
            pass
    md_json_pattern = re.compile(r'```(?:json)?\s*\n?([\s\S]*?)\n?```', re.IGNORECASE)
    for match in md_json_pattern.finditer(text):
        candidate = match.group(1).strip()
        try:
            return json.loads(candidate)
        except json.JSONDecodeError:
            pass
    return {"text": text, "_raw_text": True}


# ──────────────────────────────────────────────────────────────────────────────
# 工具参数适配
# ──────────────────────────────────────────────────────────────────────────────

_TOOLS_NEEDING_PARAMS_WRAP = frozenset({
    "twelvedata_GetApiUsage", "twelvedata_GetCommodities", "twelvedata_GetCrossListings",
    "twelvedata_GetCryptocurrencies", "twelvedata_GetCryptocurrencyExchanges",
    "twelvedata_GetCurrencyConversion", "twelvedata_GetDividends",
    "twelvedata_GetEarliestTimestamp", "twelvedata_GetEarnings", "twelvedata_GetEod",
    "twelvedata_GetEtf", "twelvedata_GetExchangeRate", "twelvedata_GetExchanges",
    "twelvedata_GetForexPairs", "twelvedata_GetFunds", "twelvedata_GetIpoCalendar",
    "twelvedata_GetLogo", "twelvedata_GetMarketState", "twelvedata_GetPrice",
    "twelvedata_GetProfile", "twelvedata_GetQuote", "twelvedata_GetSplits",
    "twelvedata_GetStocks", "twelvedata_GetSymbolSearch",
    "twelvedata_GetTechnicalIndicators", "twelvedata_GetTimeSeries",
    "twelvedata_GetTimeSeriesAdx", "twelvedata_GetTimeSeriesAtr",
    "twelvedata_GetTimeSeriesBBands", "twelvedata_GetTimeSeriesCross",
    "twelvedata_GetTimeSeriesEma", "twelvedata_GetTimeSeriesMacd",
    "twelvedata_GetTimeSeriesRsi", "twelvedata_GetTimeSeriesSma",
})

_FILE_PATH_TOOLS = frozenset({
    "desktop-commander_read_file", "desktop-commander_write_file",
    "desktop-commander_create_directory", "desktop-commander_move_file",
    "filesystem_read_file", "filesystem_read_text_file", "filesystem_write_file",
    "filesystem_get_file_info", "filesystem_move_file", "filesystem_edit_file",
    "filesystem_create_directory", "filesystem_read_multiple_files",
    "filesystem_directory_tree", "filesystem_list_directory",
    "filesystem_list_directory_with_sizes", "filesystem_search_files",
})

_SANDBOX_ALLOWED_ROOT = "/data"

_REDIRECT_PATH_PREFIXES = [
    "/project/", "/projects/",
    "/var/log/", "/var/",
    "/tmp/", "/temp/",
    "/home/", "/root/",
    "/etc/",
    "/app/", "/apps/",
    "/workspace/", "/workspaces/",
    "/src/", "/source/",
]


def _adapt_tool_args(tool_name: str, tool_args: dict) -> dict:
    """对工具参数进行运行时适配（params 包裹 + 文件路径重定向）。"""
    adapted = dict(tool_args)
    if tool_name in _TOOLS_NEEDING_PARAMS_WRAP and "params" not in adapted:
        return {"params": adapted}
    if tool_name in _FILE_PATH_TOOLS:
        for path_key in ("path", "source", "destination", "src", "dst"):
            val = adapted.get(path_key)
            if isinstance(val, str) and not val.startswith(_SANDBOX_ALLOWED_ROOT):
                for prefix in _REDIRECT_PATH_PREFIXES:
                    if val.startswith(prefix):
                        rel = val[len(prefix):]
                        adapted[path_key] = f"{_SANDBOX_ALLOWED_ROOT}/{rel}"
                        break
                else:
                    if val.startswith("/") and not val.startswith(_SANDBOX_ALLOWED_ROOT):
                        adapted[path_key] = _SANDBOX_ALLOWED_ROOT + val
    return adapted


# ──────────────────────────────────────────────────────────────────────────────
# 工具调用
# ──────────────────────────────────────────────────────────────────────────────

def call_tool(tool_name: str, tool_args: dict) -> Any:
    """同步调用 MCP Server（1984 端口）执行工具，返回 Python 对象。

    这是 GT 执行脚本和评分断言代码的统一入口。
    """
    tool_args = _adapt_tool_args(tool_name, tool_args or {})
    url = f"{MCP_SERVER_URL}/call-tool"
    payload = {"tool_name": tool_name, "tool_args": tool_args}
    try:
        resp = requests.post(url, json=payload, timeout=60)
        resp.raise_for_status()
        data = resp.json()
        if isinstance(data, list) and len(data) > 0 and isinstance(data[0], dict):
            raw_text = data[0].get("text", json.dumps(data))
            return _parse_tool_response(raw_text)
        return _parse_tool_response(data)
    except requests.exceptions.ConnectionError:
        raise RuntimeError(
            f"无法连接到 MCP Server ({url})。"
            f"请先启动：cd mcp-atlas && make run-agent-environment"
        )
    except requests.exceptions.Timeout:
        raise RuntimeError(f"工具调用超时（60s）：{tool_name}")
    except Exception as e:
        raise RuntimeError(f"工具调用失败 {tool_name}: {e}")
