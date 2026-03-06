"""
独立脚本：对「生成任务」中的 dynamic_reference_script 执行 generate_reference_answer，得到 GT 执行结果。

用途：在已有 benchmark 之外，专门用于「你修改/新增的」生成任务（如 reasoning_generated_tasks.jsonl）
的 Ground Truth 执行与结果收集，不与原有 mcp_eval 流程混淆。

用法:
  # 在项目根 mcp-atlas 下执行，保证能 import mcp_completion
  cd /path/to/mcp-atlas
  python task_generation/run_gt_execution.py --input task_generation/tasks/reasoning_generated_tasks.jsonl --output task_generation/tasks/reasoning_generated_tasks_with_gt.jsonl

  # 或指定 tasks 目录下的文件名（相对 task_generation 的 tasks/）
  python task_generation/run_gt_execution.py --input tasks/reasoning_generated_tasks.jsonl --output tasks/reasoning_generated_tasks_with_gt.jsonl

输出：与输入同结构的 jsonl，每行增加字段：
  - gt_execution_log: 成功时 generate_reference_answer() 的返回字符串
  - gt_execution_error: 失败时的错误信息（成功时为 null）
  - gt_execution_ok: true/false

注意：脚本内应使用 call_tool_sync(tool_name, args)，其中 tool_name 为完整名（如 "osm-mcp-server_search_category"）。
若生成的脚本仍为 call_tool_sync(server, tool_name, params) 三参数形式，需在 prompt 中修正或在此处做兼容。
"""

import argparse
import json
import os
import re
import sys
import traceback
import requests
from pathlib import Path
from typing import Any, Tuple

# ── MCP Server 地址（1984 端口为工具执行环境）────────────────────────────────
# 优先读取环境变量，默认指向本地 1984 端口
MCP_SERVER_URL = os.getenv("MCP_SERVER_URL", "http://localhost:1984")

# ── call_tool_sync：同步调用 MCP Server 的工具 ────────────────────────────────
# run_gt_execution.py 需要在 dynamic_reference_script 执行期间调用真实工具。
# 通过 HTTP 调用 1984 端口的 /call-tool 接口实现，无需依赖 mcp_completion 包。

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
    "twelvedata_GetStatistics", "twelvedata_GetStocks", "twelvedata_GetSymbolSearch",
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
            f"请先启动服务：cd mcp-atlas && make run-agent-environment"
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

import types as _json_wrapper_types

def _make_smart_json():
    """返回一个行为与标准 json 模块相同、但 loads() 对非 JSON 内容宽容的对象。"""
    mod = _json_wrapper_types.ModuleType("json")
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


# ── 虚拟的 mcp_client 模块对象，供猴子补丁使用 ───────────────────────────────
# dynamic_reference_script 里可能写 "from mcp_completion.mcp_client import call_tool_sync"
# 我们在运行时把这个模块注入进去，让 exec() 能找到正确的实现。
import types as _types
_mcp_client_mod = _types.ModuleType("mcp_completion.mcp_client")
_mcp_client_mod.call_tool_sync = _call_tool_sync_impl  # type: ignore
sys.modules.setdefault("mcp_completion", _types.ModuleType("mcp_completion"))
sys.modules["mcp_completion.mcp_client"] = _mcp_client_mod


def _call_tool_sync_compat(tool_name_or_server: str, tool_args_or_name=None, tool_args_3rd=None):
    """
    兼容两种用法：
    - call_tool_sync(tool_name, args)         → 直接调用（两参数）
    - call_tool_sync(server, tool_name, args) → 合并为 "server_tool_name"（三参数）
    """
    if tool_args_3rd is not None:
        # 三参数: (server, tool_name, args)
        full_name = f"{tool_name_or_server}_{tool_args_or_name}"
        return _call_tool_sync_impl(full_name, tool_args_3rd)
    # 两参数: (tool_name, args)
    return _call_tool_sync_impl(tool_name_or_server, tool_args_or_name or {})


def run_one_gt_script(task: dict, task_index: int, verbose: bool = False) -> Tuple[dict, bool]:
    """
    对单条任务执行 ground_truth.dynamic_reference_script 中的 generate_reference_answer()。
    返回 (更新后的 task 字典, 是否成功)。
    """
    out = dict(task)
    out.setdefault("gt_execution_log", None)
    out.setdefault("gt_execution_error", None)
    out.setdefault("gt_execution_ok", False)

    gt = task.get("ground_truth") or {}
    strategy = gt.get("strategy")
    script_src = (gt.get("dynamic_reference_script") or "").strip()

    if strategy != "dynamic_script" or not script_src:
        out["gt_execution_error"] = "skip: no dynamic_script or empty script"
        return out, False

    # 执行脚本：在独立 namespace 中 exec，再调用 generate_reference_answer()
    # 把 call_tool_sync 替换为兼容版（支持两参/三参）
    # 同时注入 call_tool（老式两参/三参写法），让各种脚本都能找到正确实现。
    _original_call_tool_sync = _mcp_client_mod.call_tool_sync
    _mcp_client_mod.call_tool_sync = _call_tool_sync_compat

    # 同步注入虚拟的 mcp_client 模块（脚本内 "from mcp_client import call_tool" 需要）
    import types as _t
    _mcp_client_simple = _t.ModuleType("mcp_client")
    _mcp_client_simple.call_tool = _call_tool_sync_compat       # call_tool 别名
    _mcp_client_simple.call_tool_sync = _call_tool_sync_compat  # call_tool_sync 别名
    sys.modules["mcp_client"] = _mcp_client_simple

    namespace = {
        "__builtins__": __builtins__,
        "json": _make_smart_json(),
        # 直接在 namespace 注入，让脚本里裸用 call_tool / call_tool_sync 也能工作
        "call_tool": _call_tool_sync_compat,
        "call_tool_sync": _call_tool_sync_compat,
    }
    try:
        exec(script_src, namespace)
    except Exception as e:
        _mcp_client_mod.call_tool_sync = _original_call_tool_sync
        out["gt_execution_error"] = f"exec script failed: {e}"
        if verbose:
            traceback.print_exc()
        return out, False

    fn = namespace.get("generate_reference_answer")
    if not callable(fn):
        _mcp_client_mod.call_tool_sync = _original_call_tool_sync
        out["gt_execution_error"] = "generate_reference_answer not found or not callable"
        return out, False

    try:
        result = fn()
        out["gt_execution_log"] = result if isinstance(result, str) else json.dumps(result, ensure_ascii=False)
        out["gt_execution_ok"] = True
        out["gt_execution_error"] = None
        return out, True
    except Exception as e:
        out["gt_execution_error"] = str(e)
        out["gt_execution_log"] = None
        if verbose:
            traceback.print_exc()
        return out, False
    finally:
        # 恢复 mcp_completion.mcp_client 中的 call_tool_sync
        _mcp_client_mod.call_tool_sync = _original_call_tool_sync
        # 清理临时注入的 mcp_client 简单模块，避免污染全局 sys.modules
        sys.modules.pop("mcp_client", None)


def main():
    parser = argparse.ArgumentParser(
        description="Run generate_reference_answer() from generated task jsonl and write results to a new file."
    )
    parser.add_argument(
        "--input",
        required=True,
        help="Input jsonl path (e.g. task_generation/tasks/reasoning_generated_tasks.jsonl or tasks/reasoning_generated_tasks.jsonl)",
    )
    parser.add_argument(
        "--output",
        required=True,
        help="Output jsonl path; each line = same as input + gt_execution_log, gt_execution_error, gt_execution_ok",
    )
    parser.add_argument(
        "--limit",
        type=int,
        default=None,
        help="Max number of tasks to process (default: all)",
    )
    parser.add_argument(
        "--verbose",
        action="store_true",
        help="打印每条任务失败时的完整 traceback，用于调试脚本错误",
    )
    args = parser.parse_args()

    # 相对路径均相对于当前工作目录解析
    input_path = Path(args.input)
    if not input_path.is_absolute():
        input_path = Path.cwd() / args.input
    if not input_path.exists():
        print(f"Error: input file not found: {input_path}", file=sys.stderr)
        sys.exit(1)

    output_path = Path(args.output)
    if not output_path.is_absolute():
        output_path = Path.cwd() / args.output

    tasks = []
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

    print(f"Processing {len(tasks)} tasks from {input_path}")
    print(f"Output: {output_path}")
    print("(Ensure MCP_SERVER_URL / sandbox is reachable if scripts call call_tool_sync)")

    results = []
    ok_count = 0
    for i, task in enumerate(tasks):
        out, ok = run_one_gt_script(task, i, verbose=args.verbose)
        results.append(out)
        if ok:
            ok_count += 1
            print(f"  [{i+1}/{len(tasks)}] OK  (bucket={task.get('bucket')}, difficulty={task.get('difficulty')})")
        else:
            err = out.get("gt_execution_error") or "unknown"
            # 非 verbose 模式只显示前 120 字符，verbose 模式已在函数内打印完整 traceback
            print(f"  [{i+1}/{len(tasks)}] FAIL: {err[:120]}")

    output_path.parent.mkdir(parents=True, exist_ok=True)
    with open(output_path, "w", encoding="utf-8") as f:
        for rec in results:
            f.write(json.dumps(rec, ensure_ascii=False) + "\n")

    print(f"\nDone. Success: {ok_count}/{len(tasks)}. Written to {output_path}")


if __name__ == "__main__":
    main()
