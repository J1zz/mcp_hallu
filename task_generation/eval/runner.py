"""Evaluation runner: LLM judge client, per-task scoring loop, report, and full pipeline."""

import importlib.util
import json
import logging
import os
import shutil
import subprocess as _sp
import tempfile
from pathlib import Path
from typing import Any, Dict, List, Optional

import pandas as pd

from .config import EVALS_AVAILABLE, LLMClient, EvalCfg, MCP_ATLAS_EVAL_DIR, SCRIPT_DIR
from .data_io import build_tasks_from_completion_csv, load_tasks_from_jsonl, tasks_to_csv
from .schema import Task, HallucinationType
from .scoring import route_and_score
from .trajectory import parse_model_response, parse_tool_calls_from_trajectory, parse_full_trajectory, parse_full_trajectory_from_conversation, _safe_str

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Docker /data snapshot helpers (inter-task state isolation)
# ---------------------------------------------------------------------------

# Excluded from snapshots: large venv cache that does not affect task state.
_SNAPSHOT_EXCLUDE = "/data/repos/mcp_code_executor_workspace"
_SNAPSHOT_PATH_IN_CONTAINER = "/tmp/_hallu_eval_snapshot.tar.gz"


def _docker_cmd() -> str:
    """Return the docker executable path, checking common macOS locations."""
    for c in ("docker",
              "/Volumes/Docker/Docker.app/Contents/Resources/bin/docker",
              "/usr/local/bin/docker",
              "/opt/homebrew/bin/docker"):
        if shutil.which(c):
            return c
    return "docker"


def _get_container_name() -> Optional[str]:
    """Return the name of the running agent-environment container, or None."""
    docker = _docker_cmd()
    try:
        out = _sp.check_output(
            [docker, "ps", "--format", "{{.Names}}\t{{.Image}}"],
            stderr=_sp.DEVNULL, timeout=10,
        ).decode()
        for line in out.splitlines():
            parts = line.split("\t")
            if len(parts) == 2 and "agent-environment" in parts[1]:
                return parts[0].strip()
    except Exception:
        pass
    return None


def docker_snapshot_create(container: str) -> bool:
    """Create a tar snapshot of /data inside the container (~800 ms, ~23 MB).

    The snapshot is stored at _SNAPSHOT_PATH_IN_CONTAINER inside the container.
    Returns True on success.
    """
    docker = _docker_cmd()
    cmd = (
        f"tar -czf {_SNAPSHOT_PATH_IN_CONTAINER} "
        f"--exclude={_SNAPSHOT_EXCLUDE} /data 2>/dev/null && echo OK"
    )
    try:
        out = _sp.check_output(
            [docker, "exec", container, "sh", "-c", cmd],
            stderr=_sp.PIPE, timeout=60,
        ).decode().strip()
        ok = (out == "OK")
        if ok:
            logger.info(f"[snapshot] /data snapshot created → {_SNAPSHOT_PATH_IN_CONTAINER}")
        else:
            logger.warning(f"[snapshot] snapshot creation may have failed, output: {out!r}")
        return ok
    except Exception as e:
        logger.warning(f"[snapshot] snapshot creation failed: {e}")
        return False


def docker_snapshot_restore(container: str) -> bool:
    """Restore /data from the snapshot inside the container (~250 ms).

    Deletes existing /data contents before unpacking so stale files are removed.
    Returns True on success.
    """
    docker = _docker_cmd()
    cmd = (
        f"test -f {_SNAPSHOT_PATH_IN_CONTAINER} && "
        f"find /data -mindepth 1 -not -path '{_SNAPSHOT_EXCLUDE}/*' -delete 2>/dev/null; "
        f"tar -xzf {_SNAPSHOT_PATH_IN_CONTAINER} -C / 2>/dev/null && echo OK"
    )
    try:
        out = _sp.check_output(
            [docker, "exec", container, "sh", "-c", cmd],
            stderr=_sp.PIPE, timeout=60,
        ).decode().strip()
        ok = out.endswith("OK")
        if ok:
            logger.info("[snapshot] /data restored from snapshot")
        else:
            logger.warning(f"[snapshot] restore may have failed, output: {out!r}")
        return ok
    except Exception as e:
        logger.warning(f"[snapshot] restore failed: {e}")
        return False


# ---------------------------------------------------------------------------
# LLM judge client
# ---------------------------------------------------------------------------

def build_llm_judge_client() -> Any:
    """Build an LLM judge client from environment variables; returns None if unavailable."""
    if not EVALS_AVAILABLE:
        logger.warning("mcp_evals_scores not available — LLM judge disabled")
        return None

    model   = os.getenv("EVAL_LLM_MODEL", "").strip()
    api_key = os.getenv("EVAL_LLM_API_KEY", "").strip() or os.getenv("LLM_API_KEY", "").strip()
    base    = os.getenv("EVAL_LLM_BASE_URL", "").strip() or os.getenv("LLM_BASE_URL", "").strip()

    if not model:
        logger.warning("EVAL_LLM_MODEL not set — LLM judge disabled")
        return None
    if not api_key:
        logger.warning("EVAL_LLM_API_KEY not set — LLM judge disabled")
        return None

    os.environ["EVAL_LLM_API_KEY"] = api_key
    if base:
        os.environ["EVAL_LLM_BASE_URL"] = base

    try:
        client = LLMClient(EvalCfg(evaluator_model=model, semaphore_limit=1))
        logger.info(f"LLM judge enabled: model={model}, base_url={base or '(default)'}")
        return client
    except Exception as e:
        logger.warning(f"LLM judge client creation failed: {e}")
        return None


# ---------------------------------------------------------------------------
# Inline GT generation
# ---------------------------------------------------------------------------

def _run_gt_for_task(task: Task, task_raw: dict) -> str:
    """Execute the dynamic_reference_script for a task and return gt_execution_log.

    No-ops if the task already has a gt_execution_log or uses a strategy other
    than dynamic_script. Returns an empty string on failure.
    """
    if task.gt_execution_ok and task.gt_execution_log:
        return task.gt_execution_log

    if task.ground_truth.get("strategy") != "dynamic_script":
        return task.gt_execution_log or ""

    try:
        import sys
        tg_dir = str(SCRIPT_DIR)
        if tg_dir not in sys.path:
            sys.path.insert(0, tg_dir)
        from run_gt_execution import run_one_gt_script  # type: ignore
        updated, ok = run_one_gt_script(task_raw, task_index=0, verbose=False, rollback=True)
        if ok:
            gt_log = updated.get("gt_execution_log") or ""
            logger.info(f"  [GT] dynamic_script executed successfully ({len(gt_log)} chars)")
            return gt_log
        else:
            logger.warning(f"  [GT] dynamic_script failed: {updated.get('gt_execution_error', 'unknown')}")
            return ""
    except Exception as e:
        logger.warning(f"  [GT] GT script exception: {e}")
        return ""


# ---------------------------------------------------------------------------
# Per-task scoring
# ---------------------------------------------------------------------------

def score_one_task(
    task: Task,
    task_raw: dict,
    row: Dict[str, Any],
    llm_client: Any,
    pass_threshold: float,
    json_output_dir: Optional[Path],
) -> Dict[str, Any]:
    """Run GT generation → scoring → JSON write for a single task.

    Args:
        task:            Task object (rebuilt from completion CSV; gt_execution_log may be empty).
        task_raw:        Raw task dict from the original JSONL (needed for GT script execution).
        row:             Completion CSV row dict (contains trajectory, script_model_response, etc.).
        llm_client:      LLM judge client (may be None).
        pass_threshold:  Score threshold for hallu_pass.
        json_output_dir: Directory to write per-task JSON; skipped when None.
    """
    traj_raw   = row.get("trajectory") or row.get("raw_conversation_history") or ""
    tool_calls = parse_tool_calls_from_trajectory(str(traj_raw))
    # Prefer raw_conversation_history (OpenAI format) as it carries actual tool responses;
    # fall back to the simplified trajectory field when not available.
    full_traj  = (
        parse_full_trajectory_from_conversation(str(row.get("raw_conversation_history") or ""))
        or parse_full_trajectory(str(row.get("trajectory") or ""))
    )
    response   = parse_model_response(dict(row))

    if task.ground_truth.get("strategy") == "dynamic_script" and not task.gt_execution_ok:
        gt_log = _run_gt_for_task(task, task_raw)
        if gt_log:
            task.gt_execution_log = gt_log
            task.gt_execution_ok  = True

    try:
        components    = route_and_score(
            task, tool_calls, response, len(tool_calls),
            llm_client=llm_client,
            full_trajectory=full_traj or None,
        )
        score         = components.get("score", 0.0)
        strategy_used = components.get("strategy", "unknown")
    except RuntimeError as e:
        logger.error(f"  [config/data error] {e}", exc_info=True)
        components, score, strategy_used = {"config_error": str(e), "score": 0.0}, 0.0, "config_error"
    except Exception as e:
        logger.error(f"  [scoring error] {e}", exc_info=True)
        components, score, strategy_used = {"error": str(e), "score": 0.0}, 0.0, "error"

    out = dict(row)
    out.update({
        "hallu_score":      score,
        "hallu_pass":       score >= pass_threshold,
        "hallu_strategy":   strategy_used,
        "hallu_components": json.dumps(components, ensure_ascii=False),
        "agent_step_count": len(tool_calls),
        "agent_tool_calls": json.dumps(tool_calls, ensure_ascii=False),
        "gt_execution_ok":  task.gt_execution_ok,
        "gt_execution_log": task.gt_execution_log or "",
    })

    # Replace pandas NaN floats with None so the output is valid JSON.
    import math as _math
    out = {k: (None if isinstance(v, float) and _math.isnan(v) else v) for k, v in out.items()}
    # GT_EXECUTION_LOG (uppercase) is a CSV pass-through duplicate; drop it.
    out.pop("GT_EXECUTION_LOG", None)

    if json_output_dir is not None:
        json_output_dir.mkdir(parents=True, exist_ok=True)
        safe_id   = str(task.task_id).replace("/", "_").replace("\\", "_")
        json_path = json_output_dir / f"{safe_id}.json"
        try:
            with open(json_path, "w", encoding="utf-8") as fp:
                json.dump(out, fp, ensure_ascii=False, indent=2)
        except Exception as e:
            logger.warning(f"  Failed to write JSON ({json_path}): {e}")

    return out


# ---------------------------------------------------------------------------
# Scoring from a completion CSV
# ---------------------------------------------------------------------------

def evaluate_from_completion_csv(
    completion_csv_path: str,
    output_csv_path: str,
    pass_threshold: float = 0.6,
    jsonl_path: Optional[str] = None,
    json_output_dir: Optional[str] = None,
) -> pd.DataFrame:
    """Score every row in a completion CSV, writing results as they complete.

    Args:
        completion_csv_path: Path to the agent completion CSV.
        output_csv_path:     Path for the aggregated scoring CSV output.
        pass_threshold:      Score threshold for hallu_pass.
        jsonl_path:          Original task JSONL path; needed to run GT scripts for
                             dynamic_script tasks when gt_execution_log is missing.
        json_output_dir:     Directory for per-task JSON files; defaults to
                             <output_csv_dir>/task_results/.
    """
    logger.info(f"Loading completion CSV: {completion_csv_path}")
    df    = pd.read_csv(completion_csv_path)
    tasks = build_tasks_from_completion_csv(df)
    llm   = build_llm_judge_client()

    out_csv_path = Path(output_csv_path)
    json_dir     = Path(json_output_dir) if json_output_dir else out_csv_path.parent / "task_results"

    raw_task_map: Dict[str, dict] = {}
    if jsonl_path:
        try:
            with open(jsonl_path, encoding="utf-8") as f:
                for idx, line in enumerate(f):
                    line = line.strip()
                    if not line:
                        continue
                    try:
                        obj = json.loads(line)
                        tid = obj.get("task_id") or f"{Path(jsonl_path).stem}_{idx}"
                        raw_task_map[tid] = obj
                    except json.JSONDecodeError:
                        pass
            logger.info(f"Loaded {len(raw_task_map)} raw tasks from JSONL (for GT scripts)")
        except Exception as e:
            logger.warning(f"Could not load JSONL ({jsonl_path}): {e}")

    out_csv_path.parent.mkdir(parents=True, exist_ok=True)
    result_rows: List[Dict[str, Any]] = []
    header_written = False

    for i, (task, (_, row)) in enumerate(zip(tasks, df.iterrows())):
        logger.info(
            f"[{i+1}/{len(tasks)}] {task.task_id}  "
            f"{task.hallucination_type}  {task.bucket}  {task.difficulty}"
        )
        scored_row = score_one_task(
            task=task,
            task_raw=raw_task_map.get(task.task_id, {}),
            row=dict(row),
            llm_client=llm,
            pass_threshold=pass_threshold,
            json_output_dir=json_dir,
        )
        result_rows.append(scored_row)

        try:
            pd.DataFrame([scored_row]).to_csv(
                out_csv_path, mode="a", header=not header_written, index=False,
            )
            header_written = True
        except Exception as e:
            logger.warning(f"  Failed to write summary CSV: {e}")

        logger.info(
            f"  → score={scored_row.get('hallu_score', 'N/A'):.4f}  "
            f"pass={scored_row.get('hallu_pass')}  "
            f"strategy={scored_row.get('hallu_strategy')}"
        )

    result_df = pd.DataFrame(result_rows)
    print_eval_report(result_df, pass_threshold)
    logger.info(f"Results saved to: {out_csv_path}")
    logger.info(f"Per-task JSON written to: {json_dir}/")
    return result_df


# ---------------------------------------------------------------------------
# Evaluation report
# ---------------------------------------------------------------------------

def print_eval_report(df: pd.DataFrame, pass_threshold: float):
    """Print a summary evaluation report to stdout."""
    W = 70
    print("\n" + "=" * W)
    print("  MCPHallu Evaluation Report")
    print("=" * W)

    valid = df["hallu_score"].dropna()
    if valid.empty:
        print("  No scored samples.")
        return

    print(f"  Samples      : {len(valid)}")
    print(f"  Mean score   : {valid.mean():.3f}")
    print(f"  Pass rate (≥{pass_threshold:.2f}): {(valid >= pass_threshold).mean():.1%}")

    def _section(col: str, label: str):
        if col not in df.columns:
            return
        print(f"\n  {label}")
        for key, grp in df.groupby(col):
            s = grp["hallu_score"].dropna()
            if s.empty:
                continue
            print(f"  {str(key):<28} n={len(s):3d}  avg={s.mean():.3f}  pass={(s >= pass_threshold).mean():.1%}")

    _section("HALLUCINATION_TYPE", "By hallucination type")
    _section("BUCKET", "By bucket")
    if "DIFFICULTY" in df.columns and df["DIFFICULTY"].nunique() > 1:
        _section("DIFFICULTY", "By difficulty")

    if "hallu_strategy" in df.columns:
        print("\n  Scoring strategy distribution")
        for strat, grp in df.groupby("hallu_strategy"):
            print(f"  {strat:<35} n={len(grp):3d}")

    if "gt_execution_ok" in df.columns:
        ok_n = df["gt_execution_ok"].sum()
        print(f"\n  GT execution OK: {ok_n}/{len(df)}")

    print("=" * W)


# ---------------------------------------------------------------------------
# Full pipeline: JSONL → agent execution → per-task scoring → CSV + JSON
# ---------------------------------------------------------------------------

async def run_full_pipeline(
    jsonl_path: str,
    model: str,
    output_csv: str,
    server_url: str,
    concurrency: int = 3,
    num_tasks: Optional[int] = None,
    task_indices: Optional[List[int]] = None,
    pass_threshold: float = 0.6,
    json_output_dir: Optional[str] = None,
    use_docker_snapshot: bool = False,
) -> pd.DataFrame:
    """End-to-end pipeline: JSONL → agent execution → per-task GT + scoring → CSV + JSON.

    Steps:
      1. Load JSONL tasks and convert to an intermediate CSV for mcp_completion_script.
      2. Run AsyncMCPTrajectoryGenerator to collect agent trajectories.
      3. For each completed task:
           a. Run run_one_gt_script if strategy == "dynamic_script" and GT is missing.
           b. Call route_and_score.
           c. Write per-task JSON and append to the summary CSV.

    Args:
        json_output_dir:     Per-task JSON directory; defaults to <output_csv_dir>/task_results/.
        use_docker_snapshot: When True, restore /data from a snapshot before each task to
                             isolate stateful side-effects. Forces concurrency=1.
        task_indices:        When set, run only the tasks at these 0-based row positions
                             (applied before num_tasks).
    """
    tasks = load_tasks_from_jsonl(jsonl_path)
    if task_indices is not None:
        tasks = [tasks[i] for i in task_indices if i < len(tasks)]
    if num_tasks:
        tasks = tasks[:num_tasks]

    tmp_csv  = tempfile.mktemp(suffix=".csv")
    tasks_df = tasks_to_csv(tasks, tmp_csv)

    # Use an absolute path so it survives os.chdir() below.
    completion_csv_path = (
        Path(output_csv)
        .with_name(Path(output_csv).stem + "_completion.csv")
        .resolve()
    )
    completion_csv_path.parent.mkdir(parents=True, exist_ok=True)

    orig_cwd = os.getcwd()

    # Snapshot mode setup
    _snapshot_container: Optional[str] = None
    if use_docker_snapshot:
        concurrency = 1  # snapshots require serial execution
        _snapshot_container = _get_container_name()
        if _snapshot_container:
            ok = docker_snapshot_create(_snapshot_container)
            if ok:
                logger.info(f"[snapshot] Initial /data snapshot created for container {_snapshot_container!r}")
            else:
                logger.warning("[snapshot] Initial snapshot failed — state isolation disabled")
                _snapshot_container = None
        else:
            logger.warning("[snapshot] No running agent-environment container found — snapshot mode disabled")

    # Resources shared by the inline (per-task) scoring path used in snapshot mode.
    _inline_result_rows: List[Dict[str, Any]] = []
    _inline_scored       = False
    _inline_llm: Any     = None
    _inline_raw_task_map: Dict[str, dict] = {}
    _inline_out_csv      = Path(output_csv)
    _inline_json_dir     = (
        Path(json_output_dir) if json_output_dir
        else _inline_out_csv.parent / "task_results"
    )
    _inline_header_written = False

    if _snapshot_container:
        _inline_llm = build_llm_judge_client()
        if jsonl_path:
            try:
                with open(jsonl_path, encoding="utf-8") as _f:
                    for _idx, _line in enumerate(_f):
                        _line = _line.strip()
                        if not _line:
                            continue
                        try:
                            _obj = json.loads(_line)
                            _tid = _obj.get("task_id") or f"{Path(jsonl_path).stem}_{_idx}"
                            _inline_raw_task_map[_tid] = _obj
                        except json.JSONDecodeError:
                            pass
                logger.info(f"Loaded {len(_inline_raw_task_map)} raw tasks from JSONL")
            except Exception as _e:
                logger.warning(f"Could not load JSONL ({jsonl_path}): {_e}")

    # Agent execution
    try:
        os.chdir(MCP_ATLAS_EVAL_DIR)
        (MCP_ATLAS_EVAL_DIR / "completion_results").mkdir(exist_ok=True)
        completion_csv_path.unlink(missing_ok=True)

        spec    = importlib.util.spec_from_file_location(
            "mcp_completion_script",
            MCP_ATLAS_EVAL_DIR / "mcp_completion_script.py",
        )
        mcp_mod = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(mcp_mod)
        mcp_mod.SERVER_URL = server_url

        if _snapshot_container:
            _rows_scored = 0
            async with mcp_mod.AsyncMCPTrajectoryGenerator(model) as gen:
                for task_idx, (_, row) in enumerate(tasks_df.iterrows()):
                    task_id = row.get("TASK", f"task_{task_idx}")
                    logger.info(f"[snapshot] Restoring /data (task {task_idx+1}/{len(tasks_df)}: {task_id})")
                    docker_snapshot_restore(_snapshot_container)

                    await gen.evaluate_dataset_async(
                        pd.DataFrame([row.to_dict()]), str(completion_csv_path), None, 1
                    )

                    try:
                        os.chdir(orig_cwd)
                        comp_df  = pd.read_csv(completion_csv_path)
                        new_rows = comp_df.iloc[_rows_scored:]
                        if not new_rows.empty:
                            task_objs = build_tasks_from_completion_csv(new_rows)
                            for t_obj, (_, comp_row) in zip(task_objs, new_rows.iterrows()):
                                t_raw = _inline_raw_task_map.get(t_obj.task_id, {})
                                logger.info(
                                    f"[{task_idx+1}/{len(tasks_df)}] {t_obj.task_id}  "
                                    f"{t_obj.hallucination_type}  {t_obj.bucket}  {t_obj.difficulty}"
                                )
                                scored = score_one_task(
                                    t_obj, t_raw, dict(comp_row),
                                    _inline_llm, pass_threshold, _inline_json_dir,
                                )
                                _inline_result_rows.append(scored)
                                pd.DataFrame([scored]).to_csv(
                                    _inline_out_csv, mode="a",
                                    header=not _inline_header_written, index=False,
                                )
                                _inline_header_written = True
                                logger.info(
                                    f"  → score={scored.get('hallu_score', 'N/A'):.4f}  "
                                    f"pass={scored.get('hallu_pass')}  "
                                    f"strategy={scored.get('hallu_strategy')}"
                                )
                            _rows_scored += len(new_rows)
                    except Exception as _se:
                        logger.warning(f"  [inline scoring] Failed for task {task_idx+1}: {_se}")
                    finally:
                        os.chdir(MCP_ATLAS_EVAL_DIR)

            _inline_scored = True
        else:
            async with mcp_mod.AsyncMCPTrajectoryGenerator(model) as gen:
                await gen.evaluate_dataset_async(
                    tasks_df, str(completion_csv_path), None, concurrency
                )

    except Exception as e:
        logger.error(f"Agent execution failed: {e} — scoring with empty trajectories")
        empty = [
            {
                **dict(r),
                "script_model_response":    "",
                "raw_conversation_history": "[]",
                "trajectory":               "[]",
                "errors":                   "[]",
                "trajectory_time":          0.0,
                "num_retry":                0,
            }
            for _, r in tasks_df.iterrows()
        ]
        pd.DataFrame(empty).to_csv(completion_csv_path, index=False)
    finally:
        os.chdir(orig_cwd)
        if _snapshot_container:
            logger.info("[snapshot] All tasks done — restoring container /data to initial state")
            docker_snapshot_restore(_snapshot_container)

    if _inline_scored:
        result_df = pd.DataFrame(_inline_result_rows)
        print_eval_report(result_df, pass_threshold)
        logger.info(f"Results saved to: {_inline_out_csv}")
        logger.info(f"Per-task JSON written to: {_inline_json_dir}/")
    else:
        result_df = evaluate_from_completion_csv(
            completion_csv_path=str(completion_csv_path),
            output_csv_path=output_csv,
            pass_threshold=pass_threshold,
            jsonl_path=jsonl_path,
            json_output_dir=json_output_dir,
        )

    try:
        Path(tmp_csv).unlink(missing_ok=True)
    except Exception:
        pass

    return result_df
