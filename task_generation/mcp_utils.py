"""mcp_utils.py — shared low-level MCP tool-call logic

Extracted from run_gt_execution.py for shared use by GT execution (run_gt_execution.py)
and scoring (eval/scoring.py), avoiding duplicated maintenance.
"""

import json
import os
import re
import time
from pathlib import Path
from typing import Any

import requests
from dotenv import load_dotenv

# Load mcp-atlas/.env first so that MCP_SERVER_URL etc. are available before module-level reads
_env_path = Path(__file__).resolve().parent.parent / "mcp-atlas" / ".env"
load_dotenv(_env_path if _env_path.exists() else None)

MCP_SERVER_URL = os.getenv("MCP_SERVER_URL", "http://localhost:19841")


# ──────────────────────────────────────────────────────────────────────────────
# Tool response parsing
# ──────────────────────────────────────────────────────────────────────────────

def _parse_tool_response(raw: Any) -> Any:
    """Normalise a tool response of any format to a Python object (dict/list) or raw string."""
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
# Tool argument adaptation
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
    """Apply runtime adaptations to tool arguments (params wrapping + file-path redirection)."""
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
# Tool invocation
# ──────────────────────────────────────────────────────────────────────────────

def call_tool(tool_name: str, tool_args: dict, _retries: int = 3, _backoff: float = 2.0) -> Any:
    """Synchronously invoke a tool via the MCP Server and return a Python object.

    This is the unified entry-point for GT execution scripts and scoring assertion code.
    Automatically retries with exponential backoff on transient errors (500/429 rate-limit
    proxy wrapping), up to _retries attempts.
    """
    tool_args = _adapt_tool_args(tool_name, tool_args or {})
    url = f"{MCP_SERVER_URL}/call-tool"
    payload = {"tool_name": tool_name, "tool_args": tool_args}
    last_exc: Exception = RuntimeError("unknown")
    for attempt in range(_retries):
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
                f"Cannot connect to MCP Server ({url}). "
                f"Start it first: cd mcp-atlas && make run-agent-environment"
            )
        except requests.exceptions.Timeout:
            # Retry timeouts once with a short backoff; they may be transient
            if attempt < _retries - 1:
                time.sleep(_backoff)
                last_exc = RuntimeError(f"Tool call timed out (60s): {tool_name}")
                continue
            raise RuntimeError(f"Tool call timed out (60s): {tool_name}")
        except requests.exceptions.HTTPError as e:
            status = e.response.status_code if e.response is not None else 0
            # Retry on 429 (rate limit) and 500/502/503 (transient server errors)
            if status in (429, 500, 502, 503) and attempt < _retries - 1:
                wait = _backoff * (2 ** attempt)
                time.sleep(wait)
                last_exc = RuntimeError(f"Tool call failed {tool_name}: {e}")
                continue
            raise RuntimeError(f"Tool call failed {tool_name}: {e}")
        except Exception as e:
            raise RuntimeError(f"Tool call failed {tool_name}: {e}")
    raise last_exc
