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
import sys
import traceback
import types as _types
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional, Tuple

from mcp_utils import (
    MCP_SERVER_URL,
    _adapt_tool_args,
    _parse_tool_response,
    call_tool as _call_tool_sync_impl,
)


# ──────────────────────────────────────────────────────────────────────────────
# 1. 智能 json 模块：注入到脚本 namespace，让 json.loads() 不再崩溃 ─────────────
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

    # ── 文件系统写操作（最常见，memory/reasoning 任务大量使用）────────────────
    # 写文件 → 删除该文件（若文件在任务前不存在则删除即可还原；若已存在则会误删）
    # 注意：此处采用「删除」语义，适合 GT 脚本创建新文件的场景。
    # 若任务是「覆盖已有文件」，删除会改变状态——但 GT 脚本通常创建新文件，风险可接受。
    "desktop-commander_write_file": lambda args, res: (
        "desktop-commander_delete_file",
        {"path": args.get("path")},
    ) if args.get("path") else None,

    "filesystem_write_file": lambda args, res: (
        "filesystem_delete_file",
        {"path": args.get("path")},
    ) if args.get("path") else None,

    "desktop-commander_edit_block": lambda args, res: None,  # 覆盖编辑无法自动逆向，跳过

    # 创建目录 → 删除该目录（仅在目录为空时有效；若任务往里写了文件则先被上面规则回滚）
    "desktop-commander_create_directory": lambda args, res: (
        "desktop-commander_delete_file",  # desktop-commander 用 delete_file 删目录
        {"path": args.get("path")},
    ) if args.get("path") else None,

    "filesystem_create_directory": lambda args, res: (
        "filesystem_delete_directory",
        {"path": args.get("path"), "recursive": True},
    ) if args.get("path") else None,

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
    """执行 ground_truth 脚本/断言，将结果写入 gt_execution_log。

    - dynamic_script：执行 generate_reference_answer()，回滚写操作。
    - state_check：逐条执行 state_assertions 里的 code（注入 call_tool），
                   把各断言实际值记录到 gt_execution_log。

    返回 (更新后的 task 字典, 是否成功)。
    """
    out = dict(task)
    out.setdefault("gt_execution_log", None)
    out.setdefault("gt_execution_error", None)
    out.setdefault("gt_execution_ok", False)

    gt = task.get("ground_truth") or {}
    strategy = gt.get("strategy")

    # ── state_check 分支 ──────────────────────────────────────────────────────
    if strategy == "state_check":
        return _run_state_check_gt(task, out, gt, verbose)

    # ── dynamic_script 分支 ───────────────────────────────────────────────────
    script_src = (gt.get("dynamic_reference_script") or "").strip()
    if strategy != "dynamic_script" or not script_src:
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


def _run_state_check_gt(
    task: dict,
    out: dict,
    gt: dict,
    verbose: bool,
) -> Tuple[dict, bool]:
    """执行 state_check 类型任务的 GT pre-run：逐条 exec state_assertions 代码，
    注入 call_tool，读取 namespace['result']，记录实际值到 gt_execution_log。
    """
    from mcp_utils import call_tool as _call_tool

    assertions = gt.get("state_assertions") or []
    if not assertions:
        out["gt_execution_error"] = "state_check: state_assertions is empty"
        return out, False

    assertion_results = []
    exec_errors = 0

    for a in assertions:
        if not isinstance(a, dict):
            continue
        code = (a.get("code") or "").strip()
        expected = a.get("expected", True)
        desc = a.get("description", "")
        if not code:
            continue

        import os as _os_mod
        import re as _re_mod
        from pathlib import Path as _Path
        namespace: Dict[str, Any] = {
            "__builtins__": __builtins__,
            "json":     json,
            "os":       _os_mod,
            "re":       _re_mod,
            "Path":     _Path,
            "call_tool": _call_tool,
        }
        try:
            exec(code, namespace)
            actual = namespace.get("result")
            if actual is None:
                raise ValueError("code did not assign to 'result'")
            passed = bool(actual) == bool(expected)
            assertion_results.append({
                "description": desc,
                "actual": actual,
                "expected": expected,
                "passed": passed,
            })
        except Exception as e:
            exec_errors += 1
            assertion_results.append({
                "description": desc,
                "exec_error": str(e),
                "expected": expected,
                "passed": False,
            })
            if verbose:
                traceback.print_exc()

    out["gt_execution_log"] = json.dumps(assertion_results, ensure_ascii=False)
    out["gt_execution_ok"] = exec_errors == 0
    if exec_errors:
        out["gt_execution_error"] = f"{exec_errors}/{len(assertion_results)} assertion(s) raised exec_error"
    else:
        out["gt_execution_error"] = None
    return out, out["gt_execution_ok"]


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
