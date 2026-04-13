"""run_gt_execution.py — GT (Ground Truth) 执行脚本

从 task JSONL 文件中读取 dynamic_reference_script，逐条执行
generate_reference_answer()，将执行日志（gt_execution_log）写回输出 JSONL。

用法:
  python run_gt_execution.py --input tasks/reasoning_generated_tasks.jsonl \\
                             --output gt/reasoning_with_gt.jsonl

选项:
  --limit N      只处理前 N 条任务（调试用）
  --no-rollback  禁用写操作补偿回滚（默认启用）
  --verbose      打印失败任务的完整 traceback
"""

import argparse
import json
import os
import re
import sys
import traceback
import types as _types
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional, Tuple

import requests

MCP_SERVER_URL = os.getenv("MCP_SERVER_URL", "http://localhost:1984")


# ──────────────────────────────────────────────────────────────────────────────
# 1. 底层工具调用
# ──────────────────────────────────────────────────────────────────────────────

def _parse_tool_response(raw: Any) -> Any:
    """
    将工具返回的任意格式统一适配为 Python 对象（dict/list）或原始字符串。

    适配规则（按优先级）：
    1. 已是 dict/list → 直接返回
    2. 字符串尝试 JSON 解析 → 成功则返回解析结果
    3. 字符串内嵌 JSON（如 Markdown 代码块里的 JSON）→ 提取并解析
    4. 纯文本（Markdown、自然语言等）→ 包装为 {"text": "...", "_raw_text": True} 返回

    这样脚本里的 json.loads() / .get() 均不会抛出 JSONDecodeError。
    """
    # 已是结构化对象，直接返回
    if isinstance(raw, (dict, list)):
        return raw

    if not isinstance(raw, str):
        # 其他类型（int/None 等）也包装一下
        return {"value": raw, "_raw_text": False}

    text = raw.strip()

    # 尝试直接 JSON 解析
    if text and text[0] in ('{', '[', '"', 't', 'f', 'n') or (text and text[0].isdigit()):
        try:
            return json.loads(text)
        except json.JSONDecodeError:
            pass

    # 尝试从 Markdown 代码块中提取 JSON
    #   ```json\n{...}\n```  或  ```\n{...}\n```
    md_json_pattern = re.compile(
        r'```(?:json)?\s*\n?([\s\S]*?)\n?```',
        re.IGNORECASE
    )
    for match in md_json_pattern.finditer(text):
        candidate = match.group(1).strip()
        try:
            return json.loads(candidate)
        except json.JSONDecodeError:
            pass

    # 兜底：纯文本，包装为带 text 字段的 dict，让脚本可以安全 .get()
    return {"text": text, "_raw_text": True}


# ── 需要将参数包裹在 params key 下的工具前缀 ─────────────────────────────────
# 这些工具的 inputSchema 要求 tool_args = {"params": {实际参数}}
# LLM 生成的脚本通常直接平铺参数，此处自动适配。
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

# ── 文件路径相关工具：沙箱只允许 /data 目录，自动将其他路径重定向 ──────────────
_FILE_PATH_TOOLS = frozenset({
    "desktop-commander_read_file", "desktop-commander_write_file",
    "desktop-commander_create_directory", "desktop-commander_move_file",
    "filesystem_read_file", "filesystem_read_text_file", "filesystem_write_file",
    "filesystem_get_file_info", "filesystem_move_file", "filesystem_edit_file",
    "filesystem_create_directory", "filesystem_read_multiple_files",
    "filesystem_directory_tree", "filesystem_list_directory",
    "filesystem_list_directory_with_sizes", "filesystem_search_files",
})

# 沙箱允许的根目录
_SANDBOX_ALLOWED_ROOT = "/data"

# 脚本中常见的、但沙箱不允许的路径前缀（按优先级匹配）
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
    """
    对工具参数进行运行时适配，解决 LLM 生成脚本与真实 Server 规范之间的差异：

    1. params 包裹适配：
       某些工具（如所有 twelvedata_* 工具）要求参数包裹在 {"params": {...}} 中。
       若 tool_args 中没有 params key，自动添加包裹。

    2. 文件路径重定向：
       沙箱文件系统只允许访问 /data 目录。
       若 tool_args 中的 path 字段指向不允许的目录（/project, /var, /tmp 等），
       自动将路径前缀替换为 /data，使脚本可以正常执行（读到的内容可能为空，但不会崩溃）。
    """
    adapted = dict(tool_args)

    # 1. params 包裹适配
    if tool_name in _TOOLS_NEEDING_PARAMS_WRAP and "params" not in adapted:
        adapted = {"params": adapted}
        return adapted  # 包裹后无需再做路径适配

    # 2. 文件路径重定向
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
                    # 兜底：若路径不以 /data 开头也不匹配任何前缀，强制加 /data 前缀
                    if val.startswith("/") and not val.startswith(_SANDBOX_ALLOWED_ROOT):
                        adapted[path_key] = _SANDBOX_ALLOWED_ROOT + val

    return adapted


def _call_tool_sync_impl(tool_name: str, tool_args: dict) -> Any:
    """
    同步调用 MCP Server（1984 端口）执行工具。

    接口：POST {MCP_SERVER_URL}/call-tool
    Body：{"tool_name": "...", "tool_args": {...}}
    返回：经过格式适配的 Python 对象（dict/list）或原始字符串。
      - 若工具返回 JSON → 解析后的 dict/list
      - 若工具返回 Markdown/纯文本 → {"text": "...", "_raw_text": True}

    需要 MCP Server 处于运行状态。
    """
    # 运行时参数适配（params 包裹 + 路径重定向）
    tool_args = _adapt_tool_args(tool_name, tool_args)
    url = f"{MCP_SERVER_URL}/call-tool"
    payload = {"tool_name": tool_name, "tool_args": tool_args}
    try:
        resp = requests.post(url, json=payload, timeout=60)
        resp.raise_for_status()
        data = resp.json()
        # 兼容两种响应格式：
        #   1. {"content": [{"type":"text","text":"..."}]}  ← sandbox_client 格式
        #   2. 直接是结果对象
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


# ── 智能 json 模块：注入到脚本 namespace，让 json.loads() 不再崩溃 ─────────────
# dynamic_reference_script 里大量写了 json.loads(raw) 来解析工具返回值。
# 现在工具返回值已由 _parse_tool_response 处理过，绝大多数情况已是 dict/list；
# 但脚本里仍可能对 dict/list 再次调用 json.loads()（如 json.loads(result) 而 result 已是 dict）。
# _make_smart_json 返回的对象：
#   - json.loads(x)：若 x 已是 dict/list 直接返回；若是字符串调用 _parse_tool_response 适配
#   - json.dumps / json.dump 等其他方法保持原样

def _make_smart_json():
    """返回一个行为与标准 json 模块相同、但 loads() 对非 JSON 内容宽容的对象。"""
    mod = _types.ModuleType("json")
    # 拷贝所有标准 json 属性
    for attr in dir(json):
        if not attr.startswith("__"):
            setattr(mod, attr, getattr(json, attr))

    def _smart_loads(s, **kwargs):
        # 已是 dict/list（call_tool 返回的已适配对象），直接返回
        if isinstance(s, (dict, list)):
            return s
        # 字符串：先尝试标准解析，失败则走 _parse_tool_response 适配
        if isinstance(s, (bytes, bytearray)):
            s = s.decode(kwargs.pop("encoding", "utf-8"), errors="replace")
        try:
            return json.loads(s, **kwargs)
        except (json.JSONDecodeError, TypeError):
            return _parse_tool_response(s)

    mod.loads = _smart_loads
    return mod


_mcp_client_mod = _types.ModuleType("mcp_completion.mcp_client")
_mcp_client_mod.call_tool_sync = _call_tool_sync_impl  # type: ignore
sys.modules.setdefault("mcp_completion", _types.ModuleType("mcp_completion"))
sys.modules["mcp_completion.mcp_client"] = _mcp_client_mod


def _call_tool_sync_compat(tool_name_or_server: str, tool_args_or_name=None, tool_args_3rd=None) -> str:
    """兼容两种调用签名：
    - call_tool_sync(tool_name, args)         两参数
    - call_tool_sync(server, tool_name, args) 三参数（server + tool_name 合并为 server_tool_name）
    """
    if tool_args_3rd is not None:
        full_name = f"{tool_name_or_server}_{tool_args_or_name}"
        return _call_tool_sync_impl(full_name, tool_args_3rd)
    return _call_tool_sync_impl(tool_name_or_server, tool_args_or_name or {})


# ──────────────────────────────────────────────────────────────────────────────
# 2. 写操作补偿（Compensating Transactions）
# ──────────────────────────────────────────────────────────────────────────────

def _extract_id(result: Any, *fields: str) -> Optional[str]:
    """从工具返回值中尝试提取资源 ID（按给定字段名顺序查找）。"""
    if isinstance(result, dict):
        for field in fields:
            if field in result:
                return str(result[field])
            for val in result.values():
                if isinstance(val, dict) and field in val:
                    return str(val[field])
    return None


def _parse_result(raw: Any) -> Any:
    """将工具原始返回字符串解析为 Python 对象，失败则原样返回。"""
    if isinstance(raw, (dict, list)):
        return raw
    try:
        return json.loads(raw)
    except (json.JSONDecodeError, TypeError):
        return raw


# 补偿映射表：tool_name → callable(args, parsed_result) → (inverse_tool, inverse_args) | None
#
# 只覆盖「可从调用结果中提取到足够回滚信息」的写操作（A 类可逆工具）。
# 对于无法自动逆向的操作（github_push_files、mongodb_drop-collection 等），
# 不在此表中注册，任务设计层面应避免在 GT 脚本中使用。
COMPENSATION_MAP: Dict[str, Callable] = {

    # ── Airtable ──────────────────────────────────────────────────────────────
    "airtable_create_record": lambda args, res: (
        "airtable_delete_record",
        {"base_id": args.get("base_id"), "table_id": args.get("table_id"),
         "record_id": _extract_id(res, "id")},
    ) if _extract_id(res, "id") else None,

    # ── MongoDB ───────────────────────────────────────────────────────────────
    "mongodb_insert-many": lambda args, res: (
        "mongodb_delete-many",
        {
            "database": args.get("database"),
            "collection": args.get("collection"),
            "filter": {"_id": {"$in": list((res.get("insertedIds") or {}).values())}},
        },
    ) if isinstance(res, dict) and res.get("insertedIds") else None,

    "mongodb_create-collection": lambda args, res: (
        "mongodb_drop-collection",
        {"database": args.get("database"), "collection": args.get("collection")},
    ),

    "mongodb_rename-collection": lambda args, res: (
        "mongodb_rename-collection",
        {"database": args.get("database"),
         "collection": args.get("newName"),
         "newName": args.get("collection")},
    ),

    # ── Notion ────────────────────────────────────────────────────────────────
    "notion_API-post-page": lambda args, res: (
        "notion_API-delete-a-block",
        {"block_id": _extract_id(res, "id")},
    ) if _extract_id(res, "id") else None,

    "notion_API-create-a-database": lambda args, res: (
        "notion_API-delete-a-block",
        {"block_id": _extract_id(res, "id")},
    ) if _extract_id(res, "id") else None,

    # ── Google Workspace ──────────────────────────────────────────────────────
    "google-workspace_create_event": lambda args, res: (
        "google-workspace_delete_event",
        {"event_id": _extract_id(res, "id"),
         "calendar_id": args.get("calendar_id", "primary")},
    ) if _extract_id(res, "id") else None,

    # ── Lara Translate ────────────────────────────────────────────────────────
    "lara-translate_create_memory": lambda args, res: (
        "lara-translate_delete_memory",
        {"id": _extract_id(res, "id")},
    ) if _extract_id(res, "id") else None,

    "lara-translate_add_translation": lambda args, res: (
        "lara-translate_delete_translation",
        {"memory_id": args.get("memory_id"), "id": _extract_id(res, "id")},
    ) if _extract_id(res, "id") else None,
}


class CompensationRegistry:
    """记录 GT 执行过程中的写操作，任务结束后按逆序执行补偿（回滚）。

    工作流：
      registry = CompensationRegistry()
      result = registry.tracked_call(tool_name, args)
      # ... 执行完毕后 ...
      registry.rollback()   ← 逆序撤销所有已记录的写操作
    """

    def __init__(self, enabled: bool = True):
        self.enabled = enabled
        self._log: List[Tuple[str, dict, Any]] = []

    def tracked_call(self, tool_name: str, args: dict) -> str:
        """调用工具；若该工具在补偿映射表中，则将结果追加到回滚日志。"""
        raw_result = _call_tool_sync_impl(tool_name, args)
        if self.enabled and tool_name in COMPENSATION_MAP:
            self._log.append((tool_name, args, _parse_result(raw_result)))
        return raw_result

    def rollback(self):
        """逆序执行所有已记录写操作的补偿调用；单条失败只记录警告，不中断整体。"""
        if not self.enabled or not self._log:
            return
        for tool_name, args, result in reversed(self._log):
            builder = COMPENSATION_MAP.get(tool_name)
            if builder is None:
                continue
            try:
                compensation = builder(args, result)
                if compensation is None:
                    continue
                inv_tool, inv_args = compensation
                # 若任意必填参数为 None，则跳过（无法安全回滚）
                if any(v is None for v in inv_args.values()):
                    print(
                        f"  [rollback skip] {tool_name}: 缺少必要参数 {inv_args}",
                        file=sys.stderr,
                    )
                    continue
                _call_tool_sync_impl(inv_tool, inv_args)
            except Exception as e:
                print(f"  [rollback warn] {tool_name} → {e}", file=sys.stderr)
        self._log.clear()


# ──────────────────────────────────────────────────────────────────────────────
# 3. 虚拟 mcp_client 模块注入（供 exec() 内的脚本导入）
# ──────────────────────────────────────────────────────────────────────────────

_mcp_client_mod = _types.ModuleType("mcp_completion.mcp_client")
_mcp_client_mod.call_tool_sync = _call_tool_sync_compat  # type: ignore
sys.modules.setdefault("mcp_completion", _types.ModuleType("mcp_completion"))
sys.modules["mcp_completion.mcp_client"] = _mcp_client_mod


# ──────────────────────────────────────────────────────────────────────────────
# 4. 单任务 GT 执行
# ──────────────────────────────────────────────────────────────────────────────

def run_one_gt_script(
    task: dict,
    task_index: int,
    verbose: bool = False,
    rollback: bool = True,
) -> Tuple[dict, bool]:
    """执行 ground_truth.dynamic_reference_script 中的 generate_reference_answer()。

    执行完毕（成功或失败）后自动回滚写操作（除非 rollback=False）。
    返回 (更新后的 task 字典, 是否成功)。
    """
    out = dict(task)
    out.setdefault("gt_execution_log", None)
    out.setdefault("gt_execution_error", None)
    out.setdefault("gt_execution_ok", False)

    gt = task.get("ground_truth") or {}
    script_src = (gt.get("dynamic_reference_script") or "").strip()

    if gt.get("strategy") != "dynamic_script" or not script_src:
        out["gt_execution_error"] = "skip: no dynamic_script or empty script"
        return out, False

    registry = CompensationRegistry(enabled=rollback)

    def _tracked_compat(tool_name_or_server, tool_args_or_name=None, tool_args_3rd=None):
        if tool_args_3rd is not None:
            full_name = f"{tool_name_or_server}_{tool_args_or_name}"
            return registry.tracked_call(full_name, tool_args_3rd)
        return registry.tracked_call(tool_name_or_server, tool_args_or_name or {})

    # 注入轻量 mcp_client 模块（兼容 "from mcp_client import call_tool" 写法）
    _mcp_client_simple = _types.ModuleType("mcp_client")
    _mcp_client_simple.call_tool = _tracked_compat        # type: ignore
    _mcp_client_simple.call_tool_sync = _tracked_compat   # type: ignore
    sys.modules["mcp_client"] = _mcp_client_simple

    namespace: Dict[str, Any] = {
        "__builtins__": __builtins__,
        "json": _make_smart_json(),
        # 注入带追踪回滚功能的函数，确保脚本里裸用 call_tool / call_tool_sync
        # 时写操作也会被 registry 记录，可在任务结束后正确回滚
        "call_tool": _tracked_compat,
        "call_tool_sync": _tracked_compat,
    }

    try:
        exec(script_src, namespace)
    except Exception as e:
        out["gt_execution_error"] = f"exec script failed: {e}"
        if verbose:
            traceback.print_exc()
        registry.rollback()
        sys.modules.pop("mcp_client", None)
        return out, False

    fn = namespace.get("generate_reference_answer")
    if not callable(fn):
        out["gt_execution_error"] = "generate_reference_answer not found or not callable"
        registry.rollback()
        sys.modules.pop("mcp_client", None)
        return out, False

    try:
        result = fn()
        out["gt_execution_log"] = (
            result if isinstance(result, str) else json.dumps(result, ensure_ascii=False)
        )
        out["gt_execution_ok"] = True
        out["gt_execution_error"] = None
        return out, True
    except Exception as e:
        out["gt_execution_error"] = str(e)
        if verbose:
            traceback.print_exc()
        return out, False
    finally:
        registry.rollback()
        sys.modules.pop("mcp_client", None)


# ──────────────────────────────────────────────────────────────────────────────
# 5. CLI 入口
# ──────────────────────────────────────────────────────────────────────────────

def main():
    parser = argparse.ArgumentParser(
        description="对生成任务执行 generate_reference_answer() 并写出 GT 结果 JSONL。"
    )
    parser.add_argument("--input", required=True, help="输入 JSONL 文件路径")
    parser.add_argument(
        "--output", required=True,
        help="输出 JSONL 文件路径（每行新增 gt_execution_log / gt_execution_error / gt_execution_ok）",
    )
    parser.add_argument("--limit", type=int, default=None, help="只处理前 N 条任务")
    parser.add_argument("--no-rollback", action="store_true", help="禁用写操作补偿回滚")
    parser.add_argument("--verbose", action="store_true", help="打印失败任务的完整 traceback")
    args = parser.parse_args()

    input_path = Path(args.input)
    if not input_path.is_absolute():
        input_path = Path.cwd() / args.input
    if not input_path.exists():
        print(f"Error: input file not found: {input_path}", file=sys.stderr)
        sys.exit(1)

    output_path = Path(args.output)
    if not output_path.is_absolute():
        output_path = Path.cwd() / args.output

    tasks: List[dict] = []
    with open(input_path, "r", encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if not line:
                continue
            try:
                tasks.append(json.loads(line))
            except json.JSONDecodeError as e:
                print(f"Warning: skip invalid json line: {e}", file=sys.stderr)

    if args.limit is not None:
        tasks = tasks[: args.limit]

    rollback_enabled = not args.no_rollback
    print(f"Processing {len(tasks)} tasks from {input_path}")
    print(f"Output: {output_path}")
    print(f"Rollback: {'enabled' if rollback_enabled else 'disabled'}")

    results = []
    ok_count = 0
    for i, task in enumerate(tasks):
        out, ok = run_one_gt_script(task, i, verbose=args.verbose, rollback=rollback_enabled)
        results.append(out)
        if ok:
            ok_count += 1
            print(
                f"  [{i+1}/{len(tasks)}] OK  "
                f"(bucket={task.get('bucket')}, difficulty={task.get('difficulty')})"
            )
        else:
            err = out.get("gt_execution_error") or "unknown"
            print(f"  [{i+1}/{len(tasks)}] FAIL: {err[:120]}")

    output_path.parent.mkdir(parents=True, exist_ok=True)
    with open(output_path, "w", encoding="utf-8") as f:
        for rec in results:
            f.write(json.dumps(rec, ensure_ascii=False) + "\n")

    print(f"\nDone. Success: {ok_count}/{len(tasks)}. Written to {output_path}")


if __name__ == "__main__":
    main()
