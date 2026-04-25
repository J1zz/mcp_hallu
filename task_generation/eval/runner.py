"""主评测流程：LLM Judge 客户端、评分循环、报告输出、完整 pipeline。

新流程设计
----------
每条任务独立完成「Agent 执行 → GT 生成 → 打分 → 写出 JSON」四步，
不再等到所有任务跑完才统一打分。

GT 生成策略
-----------
- Confusion Trap / Void Trap：strategy="none" 或无 ground_truth，无需 GT，直接打分。
- Memory / Reasoning Trap，strategy="state_check"：
    state_assertions 在任务设计时已确定，同样直接打分（断言在 score_state_assertions 里 exec）。
- Memory / Reasoning Trap，strategy="dynamic_script"：
    Agent 跑完后用 eval 模型（EVAL_LLM_MODEL）作为 judge 的同一个模型——实际上
    dynamic_script 不依赖 LLM，而是同步调用 MCP 工具生成执行日志（run_one_gt_script）；
    该步骤在 agent 完成后、打分前自动执行。
"""

import importlib.util
import json
import logging
import os
import tempfile
from pathlib import Path
from typing import Any, Dict, List, Optional

import pandas as pd

from .config import EVALS_AVAILABLE, LLMClient, EvalCfg, MCP_ATLAS_EVAL_DIR, SCRIPT_DIR
from .data_io import build_tasks_from_completion_csv, load_tasks_from_jsonl, tasks_to_csv
from .schema import Task, HallucinationType
from .scoring import route_and_score
from .trajectory import parse_model_response, parse_tool_calls_from_trajectory, _safe_str

logger = logging.getLogger(__name__)


# ──────────────────────────────────────────────────────────────────────────────
# LLM Judge 客户端构建
# ──────────────────────────────────────────────────────────────────────────────

def build_llm_judge_client() -> Any:
    """从 .env 配置创建 LLM Judge 客户端；不可用时返回 None。"""
    if not EVALS_AVAILABLE:
        logger.warning("mcp_evals_scores 不可用，LLM Judge 已禁用")
        return None

    model   = os.getenv("EVAL_LLM_MODEL", "").strip()
    api_key = os.getenv("EVAL_LLM_API_KEY", "").strip() or os.getenv("LLM_API_KEY", "").strip()
    base    = os.getenv("EVAL_LLM_BASE_URL", "").strip() or os.getenv("LLM_BASE_URL", "").strip()

    if not model:
        logger.warning("EVAL_LLM_MODEL 未配置，LLM Judge 已禁用")
        return None
    if not api_key:
        logger.warning("EVAL_LLM_API_KEY 未配置，LLM Judge 已禁用")
        return None

    os.environ["EVAL_LLM_API_KEY"] = api_key
    if base:
        os.environ["EVAL_LLM_BASE_URL"] = base

    try:
        client = LLMClient(EvalCfg(evaluator_model=model, semaphore_limit=1))
        logger.info(f"LLM Judge 已启用: model={model}, base_url={base or '(default)'}")
        return client
    except Exception as e:
        logger.warning(f"LLM Judge 客户端创建失败: {e}")
        return None


# ──────────────────────────────────────────────────────────────────────────────
# GT 生成（inline，集成到 eval 流程）
# ──────────────────────────────────────────────────────────────────────────────

def _run_gt_for_task(task: Task, task_raw: dict) -> str:
    """对 dynamic_script 任务在 agent 跑完后内联执行 GT 脚本，返回 gt_execution_log。

    仅对 strategy=="dynamic_script" 且含有 dynamic_reference_script 的任务执行；
    其余情况返回空字符串（task 已经携带 gt_execution_log 则直接返回）。
    """
    # 已经有 gt_execution_log（例如从带 GT 的 JSONL 加载进来的任务）
    if task.gt_execution_ok and task.gt_execution_log:
        return task.gt_execution_log

    strategy = task.ground_truth.get("strategy", "")
    if strategy != "dynamic_script":
        return task.gt_execution_log or ""

    # 调用 run_gt_execution 里的 run_one_gt_script
    try:
        import sys
        tg_dir = str(SCRIPT_DIR)
        if tg_dir not in sys.path:
            sys.path.insert(0, tg_dir)
        from run_gt_execution import run_one_gt_script  # type: ignore
        updated, ok = run_one_gt_script(task_raw, task_index=0, verbose=False, rollback=True)
        if ok:
            gt_log = updated.get("gt_execution_log") or ""
            logger.info(f"  [GT] dynamic_script 执行成功 ({len(gt_log)} chars)")
            return gt_log
        else:
            err = updated.get("gt_execution_error", "unknown")
            logger.warning(f"  [GT] dynamic_script 执行失败: {err}")
            return ""
    except Exception as e:
        logger.warning(f"  [GT] GT 脚本执行异常: {e}")
        return ""


# ──────────────────────────────────────────────────────────────────────────────
# 单任务评分（含 GT 生成）
# ──────────────────────────────────────────────────────────────────────────────

def score_one_task(
    task: Task,
    task_raw: dict,
    row: Dict[str, Any],
    llm_client: Any,
    pass_threshold: float,
    json_output_dir: Optional[Path],
) -> Dict[str, Any]:
    """对单条任务执行「GT 生成 → 打分 → 写 JSON」三步，返回带评分字段的行字典。

    Parameters
    ----------
    task          : Task 对象（从 completion CSV 重建，gt_execution_log 可能为空）
    task_raw      : 原始任务 dict（含 ground_truth.dynamic_reference_script，供 GT 脚本使用）
    row           : completion CSV 对应行（含 trajectory / script_model_response 等）
    llm_client    : LLM Judge 客户端（可为 None）
    pass_threshold: 通过分数线
    json_output_dir: 每任务 JSON 结果写出目录（None 则不写）
    """
    traj_raw   = row.get("trajectory") or row.get("raw_conversation_history") or ""
    tool_calls = parse_tool_calls_from_trajectory(str(traj_raw))
    response   = parse_model_response(dict(row))

    # ── GT 生成（仅 dynamic_script 需要且尚未执行）──────────────────────────
    strategy = task.ground_truth.get("strategy", "")
    if strategy == "dynamic_script" and not task.gt_execution_ok:
        gt_log = _run_gt_for_task(task, task_raw)
        if gt_log:
            task.gt_execution_log = gt_log
            task.gt_execution_ok  = True

    # ── 打分 ────────────────────────────────────────────────────────────────
    try:
        components = route_and_score(task, tool_calls, response, len(tool_calls), llm_client=llm_client)
        score      = components.get("score", 0.0)
        strategy_used = components.get("strategy", "unknown")
    except Exception as e:
        logger.error(f"  评分出错: {e}")
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

    # ── 写出单任务 JSON ──────────────────────────────────────────────────────
    if json_output_dir is not None:
        json_output_dir.mkdir(parents=True, exist_ok=True)
        safe_id = str(task.task_id).replace("/", "_").replace("\\", "_")
        json_path = json_output_dir / f"{safe_id}.json"
        try:
            with open(json_path, "w", encoding="utf-8") as fp:
                json.dump(out, fp, ensure_ascii=False, indent=2)
        except Exception as e:
            logger.warning(f"  写出 JSON 失败 ({json_path}): {e}")

    return out


# ──────────────────────────────────────────────────────────────────────────────
# 从 completion CSV 评分（支持「一任务一打分」模式）
# ──────────────────────────────────────────────────────────────────────────────

def evaluate_from_completion_csv(
    completion_csv_path: str,
    output_csv_path: str,
    pass_threshold: float = 0.6,
    jsonl_path: Optional[str] = None,
    json_output_dir: Optional[str] = None,
) -> pd.DataFrame:
    """从 completion CSV 逐任务评分，每条任务完成后立即写出 JSON 并追加到汇总 CSV。

    Parameters
    ----------
    completion_csv_path : Agent 执行结果 CSV 路径
    output_csv_path     : 最终汇总评分 CSV 输出路径
    pass_threshold      : 通过分数线
    jsonl_path          : 原始任务 JSONL 路径（用于为 dynamic_script 任务提供 GT 脚本原文）
    json_output_dir     : 每任务 JSON 输出目录；None 则使用 output_csv 同目录下的 task_results/
    """
    logger.info(f"Loading completion CSV: {completion_csv_path}")
    df    = pd.read_csv(completion_csv_path)
    tasks = build_tasks_from_completion_csv(df)
    llm   = build_llm_judge_client()

    # 确定 JSON 输出目录
    out_csv_path = Path(output_csv_path)
    json_dir = Path(json_output_dir) if json_output_dir else out_csv_path.parent / "task_results"

    # 从原始 JSONL 建立 task_id → raw_task_dict 映射（用于 GT 脚本执行）
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
            logger.info(f"从 JSONL 加载了 {len(raw_task_map)} 条原始任务（用于 GT 脚本）")
        except Exception as e:
            logger.warning(f"无法加载原始 JSONL ({jsonl_path}): {e}")

    # 准备汇总输出文件（首条写入时创建 header）
    out_csv_path.parent.mkdir(parents=True, exist_ok=True)
    result_rows: List[Dict[str, Any]] = []
    header_written = False

    for i, (task, (_, row)) in enumerate(zip(tasks, df.iterrows())):
        logger.info(
            f"[{i+1}/{len(tasks)}] {task.task_id}  "
            f"{task.hallucination_type}  {task.bucket}  {task.difficulty}"
        )

        # 取对应的原始任务 dict（供 GT 脚本使用；找不到则用空 dict）
        task_raw = raw_task_map.get(task.task_id, {})

        scored_row = score_one_task(
            task=task,
            task_raw=task_raw,
            row=dict(row),
            llm_client=llm,
            pass_threshold=pass_threshold,
            json_output_dir=json_dir,
        )
        result_rows.append(scored_row)

        # 立即追加到汇总 CSV（逐行写，边跑边存）
        try:
            row_df = pd.DataFrame([scored_row])
            row_df.to_csv(
                out_csv_path,
                mode="a",
                header=not header_written,
                index=False,
            )
            header_written = True
        except Exception as e:
            logger.warning(f"  写入汇总 CSV 失败: {e}")

        logger.info(
            f"  → score={scored_row.get('hallu_score', 'N/A'):.4f}  "
            f"pass={scored_row.get('hallu_pass')}  "
            f"strategy={scored_row.get('hallu_strategy')}"
        )

    result_df = pd.DataFrame(result_rows)
    print_eval_report(result_df, pass_threshold)
    logger.info(f"评测结果已保存至: {out_csv_path}")
    logger.info(f"单任务 JSON 已写出至: {json_dir}/")
    return result_df


# ──────────────────────────────────────────────────────────────────────────────
# 评测报告
# ──────────────────────────────────────────────────────────────────────────────

def print_eval_report(df: pd.DataFrame, pass_threshold: float):
    """打印评测统计报告。"""
    W = 70
    print("\n" + "=" * W)
    print("  幻觉感知评测报告")
    print("=" * W)

    valid = df["hallu_score"].dropna()
    if valid.empty:
        print("  无有效评分数据")
        return

    print(f"  样本数量    : {len(valid)}")
    print(f"  平均得分    : {valid.mean():.3f}")
    print(f"  通过率(≥{pass_threshold:.2f}): {(valid >= pass_threshold).mean():.1%}")

    def _section(col: str, label: str):
        if col not in df.columns:
            return
        print(f"\n  ── {label} ──────────────────────────────────────")
        for key, grp in df.groupby(col):
            s = grp["hallu_score"].dropna()
            if s.empty:
                continue
            print(f"  {str(key):<28} n={len(s):3d}  avg={s.mean():.3f}  pass={(s >= pass_threshold).mean():.1%}")

    _section("HALLUCINATION_TYPE", "幻觉类型")
    _section("BUCKET", "Bucket")
    if "DIFFICULTY" in df.columns and df["DIFFICULTY"].nunique() > 1:
        _section("DIFFICULTY", "难度")

    if "hallu_strategy" in df.columns:
        print("\n  ── 评分策略分布 ─────────────────────────────────")
        for strat, grp in df.groupby("hallu_strategy"):
            print(f"  {strat:<35} n={len(grp):3d}")

    if "gt_execution_ok" in df.columns:
        ok_n = df["gt_execution_ok"].sum()
        total = len(df)
        print(f"\n  ── GT 执行状态 ──────────────────────────────────")
        print(f"  GT 执行成功: {ok_n}/{total}")

    print("=" * W)


# ──────────────────────────────────────────────────────────────────────────────
# 完整 pipeline：JSONL → Agent 执行 → 逐任务打分 → 汇总 CSV + 单任务 JSON
# ──────────────────────────────────────────────────────────────────────────────

async def run_full_pipeline(
    jsonl_path: str,
    model: str,
    output_csv: str,
    server_url: str,
    concurrency: int = 3,
    num_tasks: Optional[int] = None,
    pass_threshold: float = 0.6,
    json_output_dir: Optional[str] = None,
) -> pd.DataFrame:
    """End-to-end pipeline: JSONL → Agent execution → per-task GT + scoring → CSV + JSON。

    流程
    ----
    1. 加载 JSONL 任务，转为中间 CSV（供 mcp_completion_script 消费）
    2. 调用 AsyncMCPTrajectoryGenerator 让待测模型逐并发跑轨迹
       - 每条任务跑完后写入 completion CSV（mcp_completion_script 内部行为）
    3. 待测模型全部跑完后，逐任务：
       a. 若 strategy=="dynamic_script"，使用 run_one_gt_script 生成 GT 执行日志
       b. 调用 route_and_score 打分（dynamic_script 任务使用刚生成的 GT 日志做语义对比）
       c. 写出单任务 JSON 文件
       d. 追加到汇总 CSV

    参数
    ----
    json_output_dir : 单任务 JSON 输出目录；None 则放在 output_csv 同目录的 task_results/
    """
    tasks = load_tasks_from_jsonl(jsonl_path)
    if num_tasks:
        tasks = tasks[:num_tasks]

    tmp_csv  = tempfile.mktemp(suffix=".csv")
    tasks_df = tasks_to_csv(tasks, tmp_csv)

    # completion CSV 路径（绝对路径，避免 chdir 后相对路径错位）
    completion_csv_path = (
        Path(output_csv)
        .with_name(Path(output_csv).stem + "_completion.csv")
        .resolve()
    )
    completion_csv_path.parent.mkdir(parents=True, exist_ok=True)

    orig_cwd = os.getcwd()

    # ── Step 1: Agent 执行 ───────────────────────────────────────────────────
    try:
        os.chdir(MCP_ATLAS_EVAL_DIR)
        (MCP_ATLAS_EVAL_DIR / "completion_results").mkdir(exist_ok=True)
        completion_csv_path.unlink(missing_ok=True)

        spec = importlib.util.spec_from_file_location(
            "mcp_completion_script",
            MCP_ATLAS_EVAL_DIR / "mcp_completion_script.py",
        )
        mcp_mod = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(mcp_mod)
        mcp_mod.SERVER_URL = server_url

        async with mcp_mod.AsyncMCPTrajectoryGenerator(model) as gen:
            await gen.evaluate_dataset_async(tasks_df, str(completion_csv_path), None, concurrency)

    except Exception as e:
        logger.error(f"Agent 执行失败: {e}，将以空轨迹评分")
        empty = [
            {
                **dict(r),
                "script_model_response": "",
                "raw_conversation_history": "[]",
                "trajectory": "[]",
                "errors": "[]",
                "trajectory_time": 0.0,
                "num_retry": 0,
            }
            for _, r in tasks_df.iterrows()
        ]
        pd.DataFrame(empty).to_csv(completion_csv_path, index=False)
    finally:
        os.chdir(orig_cwd)

    # ── Step 2: 逐任务 GT 生成 + 打分 ────────────────────────────────────────
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
