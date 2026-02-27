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
import sys
from pathlib import Path
from typing import Tuple

# 保证从项目根运行时能 import mcp_completion（用于脚本内的 call_tool_sync）
SCRIPT_DIR = Path(__file__).resolve().parent
REPO_ROOT = SCRIPT_DIR.parent
MCP_EVAL_DIR = REPO_ROOT / "services" / "mcp_eval"
if str(MCP_EVAL_DIR) not in sys.path:
    sys.path.insert(0, str(MCP_EVAL_DIR))

import mcp_completion.mcp_client as _mcp_client_mod
from mcp_completion.mcp_client import call_tool_sync as _call_tool_sync_impl


def _call_tool_sync_compat(tool_name_or_server: str, tool_args_or_name=None, tool_args_3rd=None):
    """
    兼容两种用法：
    - call_tool_sync(tool_name, args)  -> 直接调用
    - call_tool_sync(server, tool_name, args) -> 转为 call_tool_sync(f"{server}_{tool_name}", args)
    """
    if tool_args_3rd is not None:
        # 三参数: (server, tool_name, args)
        full_name = f"{tool_name_or_server}_{tool_args_or_name}"
        return _call_tool_sync_impl(full_name, tool_args_3rd)
    # 两参数: (tool_name, args)
    return _call_tool_sync_impl(tool_name_or_server, tool_args_or_name or {})


def run_one_gt_script(task: dict, task_index: int) -> Tuple[dict, bool]:
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
    # 临时替换 mcp_client.call_tool_sync 为兼容版（支持两参/三参），这样脚本内
    # "from mcp_completion.mcp_client import call_tool_sync" 得到的也是兼容版
    _original_call_tool_sync = _mcp_client_mod.call_tool_sync
    _mcp_client_mod.call_tool_sync = _call_tool_sync_compat
    namespace = {"__builtins__": __builtins__, "json": json}
    try:
        exec(script_src, namespace)
    except Exception as e:
        _mcp_client_mod.call_tool_sync = _original_call_tool_sync
        out["gt_execution_error"] = f"exec script failed: {e}"
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
        return out, False
    finally:
        _mcp_client_mod.call_tool_sync = _original_call_tool_sync


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
    args = parser.parse_args()

    input_path = Path(args.input)
    if not input_path.is_absolute():
        # 若相对路径，优先相对于 repo 根，再相对于当前目录
        for base in (REPO_ROOT, Path.cwd()):
            p = base / args.input
            if p.exists():
                input_path = p
                break
        else:
            input_path = Path(args.input)
    if not input_path.exists():
        print(f"Error: input file not found: {input_path}", file=sys.stderr)
        sys.exit(1)

    output_path = Path(args.output)
    if not output_path.is_absolute():
        for base in (REPO_ROOT, Path.cwd()):
            p = base / args.output
            output_path = p
            break
        else:
            output_path = Path(args.output)

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
        out, ok = run_one_gt_script(task, i)
        results.append(out)
        if ok:
            ok_count += 1
            print(f"  [{i+1}/{len(tasks)}] OK  (bucket={task.get('bucket')}, difficulty={task.get('difficulty')})")
        else:
            err = out.get("gt_execution_error") or "unknown"
            print(f"  [{i+1}/{len(tasks)}] FAIL: {err[:80]}")

    output_path.parent.mkdir(parents=True, exist_ok=True)
    with open(output_path, "w", encoding="utf-8") as f:
        for rec in results:
            f.write(json.dumps(rec, ensure_ascii=False) + "\n")

    print(f"\nDone. Success: {ok_count}/{len(tasks)}. Written to {output_path}")


if __name__ == "__main__":
    main()
