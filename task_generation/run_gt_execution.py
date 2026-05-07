"""run_gt_execution.py — GT (Ground Truth) execution script

Reads dynamic_reference_script from task JSONL files, executes
generate_reference_answer() for each task, and writes the execution log
(gt_execution_log) back to the output JSONL.

Usage:
  python run_gt_execution.py --input tasks/reasoning_generated_tasks.jsonl \\
                             --output gt/reasoning_with_gt.jsonl

Options:
  --limit N      Process only the first N tasks (for debugging)
  --no-rollback  Disable write-operation compensating rollback (enabled by default)
  --verbose      Print full traceback for failed tasks
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
# 1. Smart json module: injected into script namespace so json.loads() no longer crashes ──────
# dynamic_reference_script extensively calls json.loads(raw) to parse tool return values.
# Tool return values have already been processed by _parse_tool_response, so most are dict/list;
# but scripts may still call json.loads() again on a dict/list
# (e.g. json.loads(result) where result is already a dict).
# The object returned by _make_smart_json:
#   - json.loads(x): if x is already dict/list, return directly; if string, adapt via _parse_tool_response
#   - json.dumps / json.dump and other methods remain unchanged

def _make_smart_json():
    """Return an object identical to the standard json module except loads() is lenient with non-JSON content."""
    mod = _types.ModuleType("json")
    # Copy all standard json attributes
    for attr in dir(json):
        if not attr.startswith("__"):
            setattr(mod, attr, getattr(json, attr))

    def _smart_loads(s, **kwargs):
        # Already dict/list (adapted object returned by call_tool), return directly
        if isinstance(s, (dict, list)):
            return s
        # String: try standard parsing first, fall back to _parse_tool_response on failure
        if isinstance(s, (bytes, bytearray)):
            s = s.decode(kwargs.pop("encoding", "utf-8"), errors="replace")
        try:
            return json.loads(s, **kwargs)
        except (json.JSONDecodeError, TypeError):
            return _parse_tool_response(s)

    mod.loads = _smart_loads
    return mod


def _call_tool_sync_compat(tool_name_or_server: str, tool_args_or_name=None, tool_args_3rd=None) -> str:
    """Support two calling signatures:
    - call_tool_sync(tool_name, args)          two-argument form
    - call_tool_sync(server, tool_name, args)  three-argument form (server + tool_name merged)
    """
    if tool_args_3rd is not None:
        full_name = f"{tool_name_or_server}_{tool_args_or_name}"
        return _call_tool_sync_impl(full_name, tool_args_3rd)
    return _call_tool_sync_impl(tool_name_or_server, tool_args_or_name or {})


# ──────────────────────────────────────────────────────────────────────────────
# 2. Write-operation Compensation (Compensating Transactions)
# ──────────────────────────────────────────────────────────────────────────────

def _extract_id(result: Any, *fields: str) -> Optional[str]:
    """Attempt to extract a resource ID from a tool return value (searches field names in given order)."""
    if isinstance(result, dict):
        for field in fields:
            if field in result:
                return str(result[field])
            for val in result.values():
                if isinstance(val, dict) and field in val:
                    return str(val[field])
    return None


def _parse_result(raw: Any) -> Any:
    """Parse the raw tool return into a Python object; return as-is on failure."""
    if isinstance(raw, (dict, list)):
        return raw
    try:
        return json.loads(raw)
    except (json.JSONDecodeError, TypeError):
        return raw


# Compensation map: tool_name → callable(args, parsed_result) → (inverse_tool, inverse_args) | None
#
# Only covers write operations where enough rollback info can be extracted from the call result
# (Class A reversible tools). Operations that cannot be automatically reversed
# (e.g. github_push_files, mongodb_drop-collection) are not registered here;
# task design should avoid using them in GT scripts.
COMPENSATION_MAP: Dict[str, Callable] = {

    # ── Filesystem write ops (most common; heavily used in memory/reasoning tasks) ────────────
    # Write file → delete that file (if file didn't exist before the task, deletion restores state;
    #                                if it already existed, deletion may be incorrect)
    # Note: using "delete" semantics; suitable for GT scripts that create new files.
    # If the task overwrites an existing file, deletion changes state — but GT scripts typically
    # create new files, so this risk is acceptable.
    "desktop-commander_write_file": lambda args, res: (
        "desktop-commander_delete_file",
        {"path": args.get("path")},
    ) if args.get("path") else None,

    "filesystem_write_file": lambda args, res: (
        "filesystem_delete_file",
        {"path": args.get("path")},
    ) if args.get("path") else None,

    "desktop-commander_edit_block": lambda args, res: None,  # Overwrite edits cannot be auto-reversed, skip

    # Create directory → delete that directory (only effective when empty;
    #                                             any files written inside are rolled back by the rules above first)
    "desktop-commander_create_directory": lambda args, res: (
        "desktop-commander_delete_file",  # desktop-commander uses delete_file to remove directories too
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
    """Records write operations during GT execution; executes compensation (rollback) in reverse order after task completion.

    Workflow:
      registry = CompensationRegistry()
      result = registry.tracked_call(tool_name, args)
      # ... after execution completes ...
      registry.rollback()   ← reverse-order undo of all recorded write operations
    """

    def __init__(self, enabled: bool = True):
        self.enabled = enabled
        self._log: List[Tuple[str, dict, Any]] = []

    def tracked_call(self, tool_name: str, args: dict) -> str:
        """Invoke a tool; if it is in the compensation map, append the result to the rollback log."""
        raw_result = _call_tool_sync_impl(tool_name, args)
        if self.enabled and tool_name in COMPENSATION_MAP:
            self._log.append((tool_name, args, _parse_result(raw_result)))
        return raw_result

    def rollback(self):
        """Execute compensation calls for all recorded write operations in reverse order;
        a single failure only logs a warning without stopping the overall rollback."""
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
                # Skip if any required argument is None (cannot safely rollback)
                if any(v is None for v in inv_args.values()):
                    print(
                        f"  [rollback skip] {tool_name}: missing required arg(s) {inv_args}",
                        file=sys.stderr,
                    )
                    continue
                _call_tool_sync_impl(inv_tool, inv_args)
            except Exception as e:
                print(f"  [rollback warn] {tool_name} → {e}", file=sys.stderr)
        self._log.clear()


# ──────────────────────────────────────────────────────────────────────────────
# 3. Virtual mcp_client module injection (for scripts imported inside exec())
# ──────────────────────────────────────────────────────────────────────────────

_mcp_client_mod = _types.ModuleType("mcp_completion.mcp_client")
_mcp_client_mod.call_tool_sync = _call_tool_sync_compat  # type: ignore
sys.modules.setdefault("mcp_completion", _types.ModuleType("mcp_completion"))
sys.modules["mcp_completion.mcp_client"] = _mcp_client_mod


# ──────────────────────────────────────────────────────────────────────────────
# 4. Single-task GT execution
# ──────────────────────────────────────────────────────────────────────────────

def run_one_gt_script(
    task: dict,
    task_index: int,
    verbose: bool = False,
    rollback: bool = True,
) -> Tuple[dict, bool]:
    """Execute the ground_truth script/assertions and write results to gt_execution_log.

    - dynamic_script: executes generate_reference_answer() and rolls back write operations.
    - state_check: execs each code snippet in state_assertions (call_tool injected),
                   recording each assertion's actual value to gt_execution_log.

    Returns (updated task dict, success flag).
    """
    out = dict(task)
    out.setdefault("gt_execution_log", None)
    out.setdefault("gt_execution_error", None)
    out.setdefault("gt_execution_ok", False)

    gt = task.get("ground_truth") or {}
    strategy = gt.get("strategy")

    # ── state_check branch ──────────────────────────────────────────────────────
    if strategy == "state_check":
        return _run_state_check_gt(task, out, gt, verbose)

    # ── dynamic_script branch ─────────────────────────────────────────────────────
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

    # Inject lightweight mcp_client module (compatible with "from mcp_client import call_tool" usage)
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
    """GT pre-run for state_check tasks: execs each state_assertions code snippet,
    injects call_tool, reads namespace['result'], and records actual values to gt_execution_log.
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
# 5. CLI entry point
# ──────────────────────────────────────────────────────────────────────────────

def main():
    parser = argparse.ArgumentParser(
        description="Execute generate_reference_answer() on generated tasks and write GT result JSONL."
    )
    parser.add_argument("--input", required=True, help="Input JSONL file path")
    parser.add_argument(
        "--output", required=True,
        help="Output JSONL file path (each line gains gt_execution_log / gt_execution_error / gt_execution_ok)",
    )
    parser.add_argument("--limit", type=int, default=None, help="Process only the first N tasks")
    parser.add_argument("--no-rollback", action="store_true", help="Disable write-operation compensating rollback")
    parser.add_argument("--verbose", action="store_true", help="Print full traceback for failed tasks")
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
