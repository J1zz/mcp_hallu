"""主评测流程：LLM Judge 客户端、评分循环、报告输出、完整 pipeline。"""

import importlib.util
import json
import logging
import os
import tempfile
from pathlib import Path
from typing import Any, Optional

import pandas as pd

from .config import EVALS_AVAILABLE, LLMClient, EvalCfg, MCP_ATLAS_EVAL_DIR
from .data_io import build_tasks_from_completion_csv, load_tasks_from_jsonl, tasks_to_csv
from .scoring import route_and_score
from .trajectory import parse_model_response, parse_tool_calls_from_trajectory

logger = logging.getLogger(__name__)


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


def evaluate_from_completion_csv(
    completion_csv_path: str,
    output_csv_path: str,
    pass_threshold: float = 0.6,
) -> pd.DataFrame:
    logger.info(f"Loading completion CSV: {completion_csv_path}")
    df    = pd.read_csv(completion_csv_path)
    tasks = build_tasks_from_completion_csv(df)
    llm   = build_llm_judge_client()

    result_rows = []
    for i, (task, (_, row)) in enumerate(zip(tasks, df.iterrows())):
        logger.info(f"[{i+1}/{len(tasks)}] {task.task_id}  {task.hallucination_type}  {task.bucket}")

        traj_raw   = row.get("trajectory") or row.get("raw_conversation_history") or ""
        tool_calls = parse_tool_calls_from_trajectory(str(traj_raw))
        response   = parse_model_response(dict(row))

        try:
            components = route_and_score(task, tool_calls, response, len(tool_calls), llm_client=llm)
            score      = components.get("score", 0.0)
            strategy   = components.get("strategy", "unknown")
        except Exception as e:
            logger.error(f"  评分出错: {e}")
            components, score, strategy = {"error": str(e), "score": 0.0}, 0.0, "error"

        out = dict(row)
        out.update({
            "hallu_score":      score,
            "hallu_pass":       score >= pass_threshold,
            "hallu_strategy":   strategy,
            "hallu_components": json.dumps(components, ensure_ascii=False),
            "agent_step_count": len(tool_calls),
            "agent_tool_calls": json.dumps(tool_calls, ensure_ascii=False),
        })
        result_rows.append(out)

    result_df = pd.DataFrame(result_rows)
    out_path  = Path(output_csv_path)
    out_path.parent.mkdir(parents=True, exist_ok=True)
    result_df.to_csv(out_path, index=False)

    print_eval_report(result_df, pass_threshold)
    logger.info(f"评测结果已保存至: {out_path}")
    return result_df


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
    if df.get("DIFFICULTY") is not None and df["DIFFICULTY"].nunique() > 1:
        _section("DIFFICULTY", "难度")

    if "hallu_strategy" in df.columns:
        print("\n  ── 评分策略分布 ─────────────────────────────────")
        for strat, grp in df.groupby("hallu_strategy"):
            print(f"  {strat:<35} n={len(grp):3d}")

    print("=" * W)


async def run_full_pipeline(
    jsonl_path: str,
    model: str,
    output_csv: str,
    server_url: str,
    concurrency: int = 3,
    num_tasks: Optional[int] = None,
    pass_threshold: float = 0.6,
) -> pd.DataFrame:
    """End-to-end pipeline: JSONL → Agent execution → Hallucination scoring → Output CSV."""
    import importlib.util

    tasks = load_tasks_from_jsonl(jsonl_path)
    if num_tasks:
        tasks = tasks[:num_tasks]

    tmp_csv  = tempfile.mktemp(suffix=".csv")
    tasks_df = tasks_to_csv(tasks, tmp_csv)

    # 使用绝对路径避免 chdir 后相对路径不在预期目录
    completion_csv_path = Path(output_csv).with_name(Path(output_csv).stem + "_completion.csv").resolve()
    completion_csv_path.parent.mkdir(parents=True, exist_ok=True)

    orig_cwd = os.getcwd()

    try:
        os.chdir(MCP_ATLAS_EVAL_DIR)
        (MCP_ATLAS_EVAL_DIR / "completion_results").mkdir(exist_ok=True)
        completion_csv_path.unlink(missing_ok=True)

        spec = importlib.util.spec_from_file_location("mcp_completion_script", MCP_ATLAS_EVAL_DIR / "mcp_completion_script.py")
        mcp_mod = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(mcp_mod)
        mcp_mod.SERVER_URL = server_url

        async with mcp_mod.AsyncMCPTrajectoryGenerator(model) as gen:
            await gen.evaluate_dataset_async(tasks_df, str(completion_csv_path), None, concurrency)

    except Exception as e:
        logger.error(f"Agent 执行失败: {e}，将以空轨迹评分")
        empty = [
            {**dict(r), "script_model_response": "", "raw_conversation_history": "[]",
             "trajectory": "[]", "errors": "[]", "trajectory_time": 0.0, "num_retry": 0}
            for _, r in tasks_df.iterrows()
        ]
        pd.DataFrame(empty).to_csv(completion_csv_path, index=False)
    finally:
        os.chdir(orig_cwd)

    result_df = evaluate_from_completion_csv(
        str(completion_csv_path), output_csv, pass_threshold
    )

    try:
        Path(tmp_csv).unlink(missing_ok=True)
    except Exception:
        pass

    return result_df
