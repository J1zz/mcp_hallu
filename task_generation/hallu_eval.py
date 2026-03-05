"""
hallu_eval.py — 幻觉类型感知的 MCP Agent 评测框架
=====================================================

功能概述
--------
本脚本实现了针对四种幻觉类型的差异化评测策略：

  幻觉1 - Confusion Trap（混淆陷阱）：【Execution】
      Agent 能否从语义相近的工具中选出正确工具？
      评分依据：是否调用了 required_tools，是否避开了 forbidden_tools。

  幻觉2 - Void Trap（空洞陷阱）*（尚未出现在生成任务中，预留支持）*：【Reasoning】
      任务本身无法被工具解决，Agent 越早识别并停止越好。
      评分依据：轨迹长度（步骤越少得分越高）。

  幻觉3 - Memory Trap（记忆陷阱）：【Memory】
      Agent 在长链路中能否记住关键信号并在后续步骤中正确使用？
      评分依据：GT 执行日志 vs 模型轨迹的语义对比（Parallel Execution）。

  幻觉4 - Reasoning Trap（推理陷阱）：【Reasoning】
      Agent 在条件分支任务中能否进行正确推理？
      评分依据：
        - 无状态/查询类（BASIC/FINANCIAL/部分ANALYTICS）：Parallel Execution 语义对比
        - 有状态/操作类（PRODUCTIVITY/CODING/部分ANALYTICS）：State Assertion 断言验证

推荐执行顺序（两步流程）
------------------------
  ┌─────────────────────────────────────────────────────────────────────┐
  │ Step 1 [hallu-gt]  预跑 GT 执行日志                                  │
  │   → 调用 MCP Server，把 dynamic_reference_script 跑一遍             │
  │   → 生成带 gt_execution_log 字段的 *_with_gt.jsonl                  │
  │                                                                     │
  │ Step 2 [hallu-eval]  幻觉评测                                        │
  │   → 读取 *_with_gt.jsonl（带 GT 日志）                               │
  │   → 让 Agent 跑任务，对比 Agent 轨迹 vs GT 日志                      │
  │   → 输出评分 CSV + 统计报告                                          │
  └─────────────────────────────────────────────────────────────────────┘

  注意：Confusion Trap / Void Trap 不需要 GT 日志，可以直接跑 hallu-eval。
        Memory Trap / Reasoning Trap 依赖 GT 日志做语义对比，必须先跑 hallu-gt。

用法示例
--------
  # ── Memory / Reasoning Trap：两步走 ─────────────────────────────────────
  # Step 1: 预跑 GT 日志（需要 MCP Server 跑在 1984 端口）
  uv run hallu-gt --input  tasks/memory_generated_tasks.jsonl --output gt/memory_with_gt.jsonl

  # Step 2: 幻觉评测（需要 MCP Agent 跑在 3000 端口）
  uv run hallu-eval --input  gt/memory_with_gt.jsonl --model  openai/gpt-5.1 --output hallu_results_memory.csv

  # ── Confusion / Void Trap：直接评测（无需 GT 日志）──────────────────────
  uv run hallu-eval \
      --input  tasks/void_tasks.jsonl \
      --model  gpt-4o-2024-05-13 \
      --output results/hallu_results_void.csv

  # ── 从已有 completion CSV 评分（跳过 Agent 执行，复用已有结果）────────────
  uv run hallu-eval \\
      --from-completion-csv completion_results/sample_51_results.csv \\
      --output hallu_results_from_existing.csv

  # ── 仅将 JSONL 转为 CSV（不跑 Agent，不评分）────────────────────────────
  uv run hallu-eval \\
      --input  tasks/memory_generated_tasks.jsonl \\
      --convert-only \\
      --output tasks_converted.csv

依赖
----
  uv sync   # 自动安装所有依赖（需 pyproject.toml 在当前目录）
"""

import argparse
import asyncio
import json
import logging
import math
import os
import sys
import time
import uuid
from dataclasses import dataclass, asdict, field
from enum import Enum
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

import pandas as pd
from dotenv import load_dotenv, find_dotenv

# ─── 路径配置 ────────────────────────────────────────────────────────────────
SCRIPT_DIR = Path(__file__).resolve().parent               # .../mcp_hallu/task_generation/
MCP_HALLU_DIR = SCRIPT_DIR.parent                          # .../mcp_hallu/
REPO_ROOT = MCP_HALLU_DIR.parent                           # .../mcp/
MCP_ATLAS_DIR = MCP_HALLU_DIR / "mcp-atlas"                # .../mcp_hallu/mcp-atlas/
MCP_ATLAS_EVAL_DIR = MCP_ATLAS_DIR / "services" / "mcp_eval"

# 将 mcp_eval 加入 sys.path，方便复用 mcp_completion 内的工具
# ⚠️ 必须在导入 mcp_evals_scores 之前完成
if str(MCP_ATLAS_EVAL_DIR) not in sys.path:
    sys.path.insert(0, str(MCP_ATLAS_EVAL_DIR))

# 加载 .env：优先使用 mcp_hallu/mcp-atlas/.env（所有配置的权威来源）
# find_dotenv() 从当前工作目录向上找，找不到侧支目录里的 .env，所以显式指定
_dotenv_path = MCP_ATLAS_DIR / ".env"
if _dotenv_path.exists():
    load_dotenv(_dotenv_path, override=True)
else:
    load_dotenv(find_dotenv())  # fallback

# ─── 可选：从 mcp_evals_scores.py 导入 LLM Judge 客户端 ─────────────────────
# sys.path 和 .env 均已就绪，现在安全导入。
# mcp_evals_scores 顶层 import 了 matplotlib，而 task_generation venv 里没有装它。
# 用 mock 临时占位，只影响本进程，不影响实际功能（LLM Judge 不依赖画图）。
try:
    import types as _types
    import sys as _sys
    _mpl_mock = _types.ModuleType("matplotlib")
    _mpl_mock.pyplot = _types.ModuleType("matplotlib.pyplot")  # type: ignore
    _sys.modules.setdefault("matplotlib", _mpl_mock)
    _sys.modules.setdefault("matplotlib.pyplot", _mpl_mock.pyplot)  # type: ignore

    from mcp_evals_scores import AsyncLiteLLMClient as _AsyncLiteLLMClient, EvaluatorConfig as _EvaluatorConfig
    _EVALS_AVAILABLE = True
except ImportError as _e:
    _AsyncLiteLLMClient = None  # type: ignore
    _EvaluatorConfig = None      # type: ignore
    _EVALS_AVAILABLE = False

# ─── 日志 ────────────────────────────────────────────────────────────────────
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    datefmt="%H:%M:%S",
)
logger = logging.getLogger(__name__)


# ═══════════════════════════════════════════════════════════════════════════════
# 1. 数据结构定义
# ═══════════════════════════════════════════════════════════════════════════════

class HallucinationType(str, Enum):
    """四种幻觉类型（与 task_generation 命名对齐）"""
    CONFUSION = "Confusion Trap"   # 混淆陷阱
    VOID      = "Void Trap"        # 空洞陷阱（任务无解，早停越好）
    MEMORY    = "Memory Trap"      # 记忆陷阱（长链路信号记忆）
    REASONING = "Reasoning Trap"   # 推理陷阱（条件分支）


# 有状态 Bucket 集合：这些类型的任务使用 State Assertion 评分
STATEFUL_BUCKETS = {"PRODUCTIVITY", "CODING"}

# 无状态 Bucket 集合：使用 Parallel Execution（语义对比）评分
STATELESS_BUCKETS = {"BASIC", "FINANCIAL", "ANALYTICS"}


@dataclass
class Task:
    """统一的任务表示（从 JSONL 解析而来）"""
    task_id: str
    bucket: str
    hallucination_type: str
    difficulty: str
    prompt: str
    available_tools: List[str]
    ground_truth: Dict[str, Any]
    evaluation_rules: Dict[str, Any]
    claims: List[Dict[str, Any]]
    should_stop_early: bool = False
    gt_execution_log: Optional[str] = None
    gt_execution_ok: bool = False


@dataclass
class EvalResult:
    """单条任务的评测结果"""
    task_id: str
    bucket: str
    hallucination_type: str
    difficulty: str
    prompt: str

    # 模型表现
    agent_trajectory_raw: Optional[str] = None     # Agent 的原始轨迹 JSON
    agent_response: Optional[str] = None           # Agent 的最终文字回复
    agent_tool_calls: List[str] = field(default_factory=list)  # 工具调用名称列表
    agent_step_count: int = 0                      # 工具调用总步数

    # 评分
    score: float = 0.0                             # 最终得分 [0, 1]
    score_components: Dict[str, Any] = field(default_factory=dict)  # 分项得分明细
    scoring_strategy: str = ""                     # 使用的评分策略名称
    pass_fail: bool = False                        # 是否通过（score >= threshold）

    # 元信息
    error: Optional[str] = None
    trajectory_time: float = 0.0
    notes: str = ""


# ═══════════════════════════════════════════════════════════════════════════════
# 2. JSONL → Task 解析 / JSONL → CSV 转换
# ═══════════════════════════════════════════════════════════════════════════════

def load_tasks_from_jsonl(jsonl_path: str) -> List[Task]:
    """
    从 task_generation/tasks/*.jsonl 文件加载任务列表。

    每行为一个 JSON 对象，字段包括：
      - bucket, hallucination_type, difficulty
      - task (prompt 文本)
      - available_tools
      - ground_truth (含 dynamic_reference_script, state_assertions 等)
      - evaluation_rules (含 required_tools, forbidden_tools 等)
      - claims
      - should_stop_early
      - gt_execution_log, gt_execution_ok（由 run_gt_execution.py 填充）
    """
    tasks = []
    path = Path(jsonl_path)
    if not path.exists():
        raise FileNotFoundError(f"JSONL 文件不存在: {jsonl_path}")

    with open(path, "r", encoding="utf-8") as f:
        for idx, line in enumerate(f):
            line = line.strip()
            if not line:
                continue
            try:
                obj = json.loads(line)
            except json.JSONDecodeError as e:
                logger.warning(f"第 {idx+1} 行 JSON 解析失败，跳过: {e}")
                continue

            task = Task(
                task_id=obj.get("task_id") or f"{path.stem}_{idx}",
                bucket=obj.get("bucket", "UNKNOWN"),
                hallucination_type=obj.get("hallucination_type", ""),
                difficulty=obj.get("difficulty", ""),
                prompt=obj.get("task", ""),
                available_tools=obj.get("available_tools", []),
                ground_truth=obj.get("ground_truth", {}),
                evaluation_rules=obj.get("evaluation_rules", {}),
                claims=obj.get("claims", []),
                should_stop_early=obj.get("should_stop_early", False),
                gt_execution_log=obj.get("gt_execution_log"),
                gt_execution_ok=bool(obj.get("gt_execution_ok", False)),
            )
            tasks.append(task)

    logger.info(f"从 {jsonl_path} 加载了 {len(tasks)} 条任务")
    return tasks


def tasks_to_csv(tasks: List[Task], output_path: str):
    """
    将任务列表转换为 mcp_completion_script.py 能直接读取的 CSV 格式。

    输出列（与 sample_tasks.csv 保持一致）：
      TASK            - 任务 ID
      ENABLED_TOOLS   - JSON 字符串，工具列表
      PROMPT          - 任务描述文本
      TRAJECTORY      - GT 执行日志（若已有则填入，否则为空）
      GTFA_CLAIMS     - JSON 字符串，claims 列表（step description + required_tool）

    额外列（供幻觉评分使用，mcp_completion_script.py 会透传不修改）：
      HALLUCINATION_TYPE  - 幻觉类型
      BUCKET              - 任务类别
      DIFFICULTY          - 难度
      SHOULD_STOP_EARLY   - 是否为早停任务（幻觉2）
      EVALUATION_RULES    - JSON 字符串，评分规则
      STATE_ASSERTIONS    - JSON 字符串，有状态任务的断言列表
    """
    rows = []
    for t in tasks:
        # 将 claims 转为 GTFA_CLAIMS 格式（字符串列表，每条为步骤描述）
        claim_strings = []
        for c in t.claims:
            if isinstance(c, dict):
                desc = c.get("description", "")
                tool = c.get("required_tool")
                claim_strings.append(f"{desc}" + (f" [requires: {tool}]" if tool else ""))
            elif isinstance(c, str):
                claim_strings.append(c)

        # GT 轨迹：若已执行过 run_gt_execution.py 则有 gt_execution_log
        gt_trajectory = t.gt_execution_log or ""

        row = {
            "TASK": t.task_id,
            "ENABLED_TOOLS": json.dumps(t.available_tools, ensure_ascii=False),
            "PROMPT": t.prompt,
            "TRAJECTORY": gt_trajectory,
            "GTFA_CLAIMS": json.dumps(claim_strings, ensure_ascii=False),
            # 幻觉评分专用列
            "HALLUCINATION_TYPE": t.hallucination_type,
            "BUCKET": t.bucket,
            "DIFFICULTY": t.difficulty,
            "SHOULD_STOP_EARLY": str(t.should_stop_early),
            "EVALUATION_RULES": json.dumps(t.evaluation_rules, ensure_ascii=False),
            "STATE_ASSERTIONS": json.dumps(
                t.ground_truth.get("state_assertions", []), ensure_ascii=False
            ),
        }
        rows.append(row)

    df = pd.DataFrame(rows)
    output_path = Path(output_path)
    output_path.parent.mkdir(parents=True, exist_ok=True)
    df.to_csv(output_path, index=False)
    logger.info(f"已将 {len(rows)} 条任务写入 CSV: {output_path}")
    return df


# ═══════════════════════════════════════════════════════════════════════════════
# 3. Agent 轨迹解析工具
# ═══════════════════════════════════════════════════════════════════════════════

def _safe_str(val: Any) -> str:
    """
    将 pandas 可能返回的 float(NaN) / None / 普通字符串统一转为字符串。
    NaN / None → 空字符串 ""。
    """
    if val is None:
        return ""
    try:
        if math.isnan(float(val)) if not isinstance(val, str) else False:
            return ""
    except (TypeError, ValueError):
        pass
    return str(val)


def parse_tool_calls_from_trajectory(trajectory_str: Optional[str]) -> List[str]:
    """
    从 AgentOutput JSON 字符串（或 mcp_completion_script.py 写入的 trajectory 列）
    提取工具调用名称列表（按调用顺序）。

    支持两种格式：
      1. AgentOutput 格式: [{type: 'message', data: {tool_calls: [{function: {name: ...}}]}}]
      2. 简化格式:         [{tool_name: ..., parameters: ..., response: ...}]
    """
    trajectory_str = _safe_str(trajectory_str)
    if not trajectory_str:
        return []

    try:
        data = json.loads(trajectory_str)
    except (json.JSONDecodeError, TypeError):
        return []

    if not isinstance(data, list):
        return []

    tool_calls = []
    for item in data:
        if not isinstance(item, dict):
            continue

        # 格式 1：AgentOutput（来自 mcp_completion_script.py 的 raw_conversation_history）
        if item.get("type") == "message":
            msg = item.get("data", {})
            for tc in (msg.get("tool_calls") or []):
                fn = tc.get("function", {})
                name = fn.get("name", "")
                if name:
                    tool_calls.append(name)

        # 格式 2：简化格式（来自 mcp_completion_script.py 的 trajectory 列）
        elif "tool_name" in item:
            name = item.get("tool_name", "")
            if name:
                tool_calls.append(name)

    return tool_calls


def parse_model_response(row: Dict[str, Any]) -> str:
    """从 CSV 行中提取模型的最终文字回复。"""
    for col in ["script_model_response", "response", "model_response"]:
        val = _safe_str(row.get(col))
        if val:
            return val
    return ""


def count_agent_steps(trajectory_str: Optional[str]) -> int:
    """统计 Agent 执行了多少步工具调用（用于早停评分）。"""
    return len(parse_tool_calls_from_trajectory(trajectory_str))


# ═══════════════════════════════════════════════════════════════════════════════
# 4. 评分策略 A：Confusion Trap 评分
# ═══════════════════════════════════════════════════════════════════════════════

def score_confusion_trap(
    task: Task,
    agent_tool_calls: List[str],
    agent_response: str,
) -> Dict[str, Any]:
    """
    混淆陷阱评分策略：
    检验 Agent 是否能从语义相近的工具中选出正确工具。

    评分维度：
      1. required_tools_hit  (0.0-1.0)：必须调用的工具覆盖率
      2. forbidden_tools_penalty (0.0-1.0)：调用了禁用工具的惩罚比例
      3. correct_tool_first  (bool)：是否「第一次」工具调用就选对了（精准度奖励）

    最终得分公式：
      score = required_hit * (1 - forbidden_penalty * 0.5) * (1.1 if correct_first else 1.0)
      并 clamp 到 [0, 1]
    """
    rules = task.evaluation_rules

    required_tools: List[str] = rules.get("required_tools", [])
    forbidden_tools: List[str] = rules.get("forbidden_tools", [])
    correct_tool: Optional[str] = rules.get("correct_tool")   # 最关键的那一个正确工具

    # 标准化：去掉前缀（有时 task 里用 "server_tool" 或 "server.tool"）
    agent_tool_set = set(agent_tool_calls)

    # 1) required_tools 命中率
    if required_tools:
        hits = sum(1 for t in required_tools if t in agent_tool_set)
        required_hit = hits / len(required_tools)
    else:
        required_hit = 1.0  # 无限制则视为满分

    # 2) forbidden_tools 惩罚率
    if forbidden_tools:
        penalty_count = sum(1 for t in forbidden_tools if t in agent_tool_set)
        forbidden_penalty = penalty_count / len(forbidden_tools)
    else:
        forbidden_penalty = 0.0

    # 3) 首选工具是否正确
    correct_first = False
    if correct_tool and agent_tool_calls:
        correct_first = (agent_tool_calls[0] == correct_tool)

    # 综合得分
    raw_score = required_hit * (1.0 - forbidden_penalty * 0.5)
    if correct_first:
        raw_score = min(1.0, raw_score * 1.1)   # 首选正确奖励 10%

    score = round(max(0.0, min(1.0, raw_score)), 4)

    return {
        "strategy": "confusion_trap",
        "required_tools": required_tools,
        "forbidden_tools": forbidden_tools,
        "agent_used_required": [t for t in required_tools if t in agent_tool_set],
        "agent_used_forbidden": [t for t in forbidden_tools if t in agent_tool_set],
        "required_hit": round(required_hit, 4),
        "forbidden_penalty": round(forbidden_penalty, 4),
        "correct_tool_first": correct_first,
        "score": score,
    }


# ═══════════════════════════════════════════════════════════════════════════════
# 5. 评分策略 B：Void Trap 早停评分
# ═══════════════════════════════════════════════════════════════════════════════

def score_void_trap(
    task: Task,
    agent_tool_calls: List[str],
    agent_response: str,
    max_allowed_steps: int = 5,
) -> Dict[str, Any]:

    steps = len(agent_tool_calls)
    quit_keywords = [
        "cannot", "can't", "unable to", "not possible", "no tool",
        "not available", "unsolvable", "无法", "不能", "没有工具", "不支持",
        "does not exist", "not exist", "no such tool",
    ]
    response_lower = agent_response.lower()
    quit_signal = any(kw in response_lower for kw in quit_keywords)

    if not quit_signal:
        base_score = 0.0
        additional_score = 0.0
        score = 0.0
    else:
        base_score = 0.5
        if steps == 0:
            additional_score = 0.5
        elif steps <= max_allowed_steps:
            additional_score = max(0.0, 0.5 - (steps * 0.1))
        else:
            additional_score = 0.0
        score = round(base_score + additional_score, 4)

    return {
        "strategy": "void_trap",
        "tool_calls_count": steps,
        "base_score": base_score,
        "additional_score": additional_score,
        "max_allowed_steps": max_allowed_steps,
        "score": score,
    }


# ═══════════════════════════════════════════════════════════════════════════════
# 6. 评分策略 C：Parallel Execution 语义对比（无状态任务）
# ═══════════════════════════════════════════════════════════════════════════════

def score_parallel_execution_sync(
    task: Task,
    agent_tool_calls: List[str],
    agent_response: str,
    llm_client=None,                # AsyncLiteLLMClient 实例（可选，用于 LLM 语义判断）
) -> Dict[str, Any]:
    """
    并行执行语义对比评分（Memory Trap / Reasoning Trap 的无状态子集）。

    策略：
      A = Agent 的实际执行结果（agent_response）
      B = GT 执行日志（task.gt_execution_log，由 run_gt_execution.py 预先生成）

    对比维度：
      1. tool_coverage  (0.0-1.0)：Agent 调用的工具是否覆盖了 required_tools
      2. semantic_match (0.0-1.0)：Agent 回复与 GT 执行日志的步骤顺序相似度
         - 若有 GT 日志且有 LLM client，使用 LLM 进行语义评分（主路径）
         - 否则使用基于 claims 的关键词匹配作为轻量 fallback
      3. branch_correct (bool)：Reasoning 任务是否走了正确的分支（检查 branch_a/branch_b 关键词）

    最终得分：
      score = tool_coverage * 0.4 + semantic_match * 0.5 + branch_correct * 0.1
    """
    rules = task.evaluation_rules
    required_tools: List[str] = rules.get("required_tools", [])
    agent_tool_set = set(agent_tool_calls)

    # ── 1) 工具覆盖率 ──────────────────────────────────────────────────────────
    if required_tools:
        hits = sum(1 for t in required_tools if t in agent_tool_set)
        tool_coverage = hits / len(required_tools)
    else:
        tool_coverage = 1.0

    # ── 2) 语义匹配 ────────────────────────────────────────────────────────────
    gt_log = _safe_str(task.gt_execution_log)
    semantic_match_method = "keyword_fallback"

    # 主路径：有 GT 日志 + 有 LLM client → 调用 LLM 进行语义评分
    if gt_log and llm_client is not None:
        try:
            llm_prompt = (
                "You are an evaluator comparing an AI agent's execution against a ground-truth execution log.\n\n"
                f"=== Ground Truth Execution Log ===\n{gt_log}\n\n"
                f"=== Agent Response ===\n{agent_response or '(empty)'}\n\n"
                f"=== Agent Tool Calls ===\n{json.dumps(agent_tool_calls)}\n\n"
                "Task description: " + task.prompt + "\n\n"
                "On a scale from 0.0 to 1.0, how semantically similar is the agent's execution to the ground truth?\n"
                "Consider: correct tools used, correct order, correct parameters intent, correct final answer.\n"
                "Respond ONLY with a JSON object: {\"score\": <float 0.0-1.0>, \"reason\": \"<brief explanation>\"}\n"
            )
            import asyncio as _asyncio
            import litellm as _litellm

            _eval_model = llm_client.config.evaluator_model

            async def _call_llm():
                # generate_structured_content 内部写死了 response_schema，
                # 该字段只有 Gemini 支持，OpenAI 会报 Unknown parameter。
                # 直接调 litellm，根据模型类型选不同的 response_format。
                _is_openai = any(_eval_model.startswith(p) for p in ("openai/", "gpt-", "o1", "o3", "o4"))
                _is_gemini = "gemini" in _eval_model.lower()

                if _is_gemini:
                    _resp_fmt = {
                        "type": "json_object",
                        "response_schema": {
                            "type": "object",
                            "properties": {
                                "score": {"type": "number"},
                                "reason": {"type": "string"},
                            },
                            "required": ["score"],
                        },
                    }
                else:
                    # OpenAI / 其他：只传 type，不传 response_schema
                    _resp_fmt = {"type": "json_object"}

                _temperature = 1 if "gpt-5" in _eval_model else 0.0
                resp = await _litellm.acompletion(
                    model=_eval_model,
                    messages=[{"role": "user", "content": llm_prompt}],
                    response_format=_resp_fmt,
                    temperature=_temperature,
                    api_key=_litellm.api_key,
                    api_base=getattr(_litellm, "api_base", None) or None,
                )
                content = resp.choices[0].message.content or "{}"
                return json.loads(content)

            # 在同步上下文中安全执行异步调用
            try:
                loop = _asyncio.get_event_loop()
                if loop.is_running():
                    import concurrent.futures
                    with concurrent.futures.ThreadPoolExecutor() as pool:
                        future = pool.submit(_asyncio.run, _call_llm())
                        llm_result = future.result(timeout=60)
                else:
                    llm_result = loop.run_until_complete(_call_llm())
            except RuntimeError:
                llm_result = _asyncio.run(_call_llm())

            raw_score_val = llm_result.get("score", 0.5)
            semantic_match = float(max(0.0, min(1.0, raw_score_val)))
            semantic_match_method = "llm_judge"
            llm_reason = llm_result.get("reason", "")
            logger.info(f"  LLM Judge 语义评分: {semantic_match:.3f} | {llm_reason[:80]}")
        except Exception as e:
            logger.warning(f"  LLM Judge 调用失败，退化到关键词匹配: {e}")
            semantic_match = None  # type: ignore
    else:
        semantic_match = None  # type: ignore

    # Fallback：基于 claims 的关键词匹配
    if semantic_match is None:
        claim_keywords = []
        for c in task.claims:
            if isinstance(c, dict):
                desc = c.get("description", "")
                # 取前 5 个英文词作为关键词
                words = [w.strip(".,;:'\"()") for w in desc.split() if len(w) > 3]
                claim_keywords.extend(words[:5])

        if claim_keywords and agent_response:
            response_lower = agent_response.lower()
            matched = sum(1 for kw in claim_keywords if kw.lower() in response_lower)
            semantic_match = min(1.0, matched / max(1, len(claim_keywords)))
        else:
            # 无法做语义匹配，给中性分
            semantic_match = 0.5
        semantic_match_method = "keyword_fallback"

    # ── 3) 分支正确性（Reasoning Trap 专属） ────────────────────────────────
    branch_correct = False
    if task.hallucination_type == HallucinationType.REASONING:
        # 从 GT 日志中检测实际触发了哪个分支
        branch_triggered = None
        if "Branch A triggered" in gt_log or "branch_a" in gt_log.lower():
            branch_triggered = "A"
        elif "Branch B triggered" in gt_log or "branch_b" in gt_log.lower():
            branch_triggered = "B"

        if branch_triggered:
            # 检查 Agent 的回复中是否提到了同一分支
            agent_resp_lower = (agent_response or "").lower()
            if branch_triggered == "A" and ("branch a" in agent_resp_lower or "more than 5" in agent_resp_lower or ">5" in agent_resp_lower):
                branch_correct = True
            elif branch_triggered == "B" and ("branch b" in agent_resp_lower or "5 or fewer" in agent_resp_lower or "expanded" in agent_resp_lower):
                branch_correct = True
        else:
            # GT 日志没有分支信息，不能判断，视为 0.5
            branch_correct = None  # type: ignore

    # ── 综合得分 ───────────────────────────────────────────────────────────────
    branch_weight = 0.1 if task.hallucination_type == HallucinationType.REASONING else 0.0
    branch_score = (1.0 if branch_correct else 0.5 if branch_correct is None else 0.0)

    raw_score = (
        tool_coverage * 0.4
        + semantic_match * (0.5 + 0.1 - branch_weight)  # 若无分支，semantic 权重更大
        + branch_score * branch_weight
    )
    score = round(max(0.0, min(1.0, raw_score)), 4)

    return {
        "strategy": "parallel_execution",
        "required_tools": required_tools,
        "agent_used_required": [t for t in required_tools if t in agent_tool_set],
        "tool_coverage": round(tool_coverage, 4),
        "semantic_match": round(semantic_match, 4),
        "semantic_match_method": semantic_match_method,
        "branch_triggered_in_gt": (
            "A" if ("Branch A triggered" in gt_log) else
            "B" if ("Branch B triggered" in gt_log) else
            "unknown"
        ),
        "agent_branch_correct": branch_correct,
        "score": score,
    }


# ═══════════════════════════════════════════════════════════════════════════════
# 7. 评分策略 D：State Assertion 断言验证（有状态任务）
# ═══════════════════════════════════════════════════════════════════════════════

def score_state_assertions(
    task: Task,
    agent_tool_calls: List[str],
    agent_response: str,
) -> Dict[str, Any]:
    """
    状态断言评分策略（有状态/操作类任务：PRODUCTIVITY / CODING）。

    原理：
      GT 中预存了 `state_assertions` 列表，每条断言是一段 Python 表达式，
      例如 "os.path.exists('/tmp/result.csv') == True"
      Agent 执行完毕后，运行这些断言表达式来验证世界状态是否正确。

    由于 Agent 运行环境与评测脚本可能不同，此处实现两种方式：

      方式 1 - 真实断言执行（需要 MCP 沙箱可访问）：
        直接 eval() 断言表达式，若抛异常则视为失败。

      方式 2 - 工具调用匹配（fallback）：
        当 state_assertions 为空或环境不可达时，
        退回到检查 required_tools 命中率 + claims 关键词匹配。

    评分：
      pass_count / total_assertions，取 [0, 1]
      若无断言（fallback 模式），使用工具覆盖率代替。
    """
    gt = task.ground_truth
    state_assertions: List[Any] = gt.get("state_assertions", [])
    rules = task.evaluation_rules
    required_tools: List[str] = rules.get("required_tools", [])
    agent_tool_set = set(agent_tool_calls)

    assertion_results = []

    # ── 方式 1：执行断言表达式 ─────────────────────────────────────────────────
    if state_assertions:
        for assertion in state_assertions:
            # 断言可以是字符串表达式，也可以是带 code/expected 的字典
            if isinstance(assertion, dict):
                expr = assertion.get("code") or assertion.get("assertion") or ""
                expected = assertion.get("expected", True)
                description = assertion.get("description", expr)
            elif isinstance(assertion, str):
                expr = assertion
                expected = True
                description = assertion
            else:
                continue

            if not expr:
                continue

            try:
                # 在安全的命名空间中执行（仅提供 os, json, re 等基础库）
                import os as _os, re as _re
                ns = {"os": _os, "json": json, "re": _re, "__builtins__": {}}
                actual = eval(expr, ns)
                passed = bool(actual) == bool(expected)
            except Exception as e:
                # 无法执行（环境不可达）时记录为失败但标注原因
                actual = None
                passed = False
                description = f"{description} [exec_error: {e}]"

            assertion_results.append({
                "description": description,
                "expression": expr,
                "expected": expected,
                "actual": str(actual),
                "passed": passed,
            })

        if assertion_results:
            pass_count = sum(1 for r in assertion_results if r["passed"])
            score = round(pass_count / len(assertion_results), 4)
            return {
                "strategy": "state_assertion",
                "assertions_total": len(assertion_results),
                "assertions_passed": pass_count,
                "assertion_details": assertion_results,
                "score": score,
                "fallback_used": False,
            }

    # ── 方式 2：Fallback — 工具覆盖率 + Claims 关键词 ──────────────────────────
    if required_tools:
        hits = sum(1 for t in required_tools if t in agent_tool_set)
        tool_coverage = hits / len(required_tools)
    else:
        tool_coverage = 1.0

    # Claims 关键词匹配
    claim_keywords = []
    for c in task.claims:
        if isinstance(c, dict):
            words = [w.strip(".,;:'\"()") for w in c.get("description", "").split() if len(w) > 3]
            claim_keywords.extend(words[:5])

    if claim_keywords and agent_response:
        matched = sum(1 for kw in claim_keywords if kw.lower() in agent_response.lower())
        keyword_score = min(1.0, matched / max(1, len(claim_keywords)))
    else:
        keyword_score = 0.5

    score = round(tool_coverage * 0.6 + keyword_score * 0.4, 4)

    return {
        "strategy": "state_assertion_fallback",
        "required_tools": required_tools,
        "agent_used_required": [t for t in required_tools if t in agent_tool_set],
        "tool_coverage": round(tool_coverage, 4),
        "keyword_score": round(keyword_score, 4),
        "score": score,
        "fallback_used": True,
        "fallback_reason": "no state_assertions in ground_truth or all exec_error",
    }


# ═══════════════════════════════════════════════════════════════════════════════
# 8. 评分路由器：根据幻觉类型 + Bucket 选择评分策略
# ═══════════════════════════════════════════════════════════════════════════════

def route_and_score(
    task: Task,
    agent_tool_calls: List[str],
    agent_response: str,
    agent_step_count: int,
    llm_client=None,                # AsyncLiteLLMClient 实例（可选，传给 parallel_execution 评分）
) -> Dict[str, Any]:
    """
    根据幻觉类型和 Bucket 自动路由到对应的评分策略。

    路由规则（与用户需求完全对应）：

    ┌─────────────────────┬────────────────────────┬─────────────────────────────┐
    │ 幻觉类型             │ Bucket                 │ 评分策略                      │
    ├─────────────────────┼────────────────────────┼─────────────────────────────┤
    │ Confusion Trap      │ 任意                   │ score_confusion_trap        │
    │ Void Trap           │ 任意                   │ score_void_trap (早停)       │
    │ Memory Trap         │ 无状态 (BASIC/FINANCIAL)│ score_parallel_execution    │
    │ Memory Trap         │ 有状态 (PROD/CODING)   │ score_state_assertions      │
    │ Memory Trap         │ ANALYTICS              │ score_parallel_execution    │
    │ Reasoning Trap      │ 无状态 (BASIC/FINANCIAL)│ score_parallel_execution    │
    │ Reasoning Trap      │ 有状态 (PROD/CODING)   │ score_state_assertions      │
    │ Reasoning Trap      │ ANALYTICS              │ score_parallel_execution    │
    └─────────────────────┴────────────────────────┴─────────────────────────────┘
    """
    h_type = task.hallucination_type
    bucket = task.bucket.upper()

    # ── Confusion Trap ─────────────────────────────────────────────────────────
    if h_type == HallucinationType.CONFUSION:
        return score_confusion_trap(task, agent_tool_calls, agent_response)

    # ── Void Trap（早停，越早停得分越高） ─────────────────────────────────────
    if h_type == HallucinationType.VOID:
        return score_void_trap(task, agent_tool_calls, agent_response)

    # ── Memory Trap / Reasoning Trap ─────────────────────────────────────────
    if bucket in STATEFUL_BUCKETS:
        # 有状态：State Assertion
        return score_state_assertions(task, agent_tool_calls, agent_response)
    else:
        # 无状态 (BASIC / FINANCIAL / ANALYTICS / 其他)：Parallel Execution（含 LLM Judge）
        return score_parallel_execution_sync(task, agent_tool_calls, agent_response, llm_client=llm_client)


# ═══════════════════════════════════════════════════════════════════════════════
# 9. 从 completion_results CSV 构建 Task 对象
# ═══════════════════════════════════════════════════════════════════════════════

def build_tasks_from_completion_csv(df: pd.DataFrame) -> List[Task]:
    """
    从 mcp_completion_script.py 输出的 completion_results CSV 重建 Task 对象。

    该 CSV 可能来自两个来源：
      a) 由 tasks_to_csv() 生成后经 mcp_completion_script.py 追加了执行结果
      b) 原始 mcp-atlas sample_tasks.csv（无幻觉类型列）

    若无幻觉类型列，则所有任务默认为 Memory Trap。
    """
    tasks = []
    for idx, row in df.iterrows():
        # 安全读取可选幻觉字段
        h_type = str(row.get("HALLUCINATION_TYPE", "Memory Trap") or "Memory Trap")
        bucket = str(row.get("BUCKET", "ANALYTICS") or "ANALYTICS")
        difficulty = str(row.get("DIFFICULTY", "") or "")

        # 解析 evaluation_rules
        eval_rules_str = row.get("EVALUATION_RULES", "{}")
        try:
            eval_rules = json.loads(eval_rules_str) if eval_rules_str else {}
        except Exception:
            eval_rules = {}

        # 解析 state_assertions
        assertions_str = row.get("STATE_ASSERTIONS", "[]")
        try:
            assertions = json.loads(assertions_str) if assertions_str else []
        except Exception:
            assertions = []

        # 解析 claims（GTFA_CLAIMS 列）
        claims_str = row.get("GTFA_CLAIMS", "[]")
        try:
            claims_raw = json.loads(claims_str) if claims_str else []
            claims = [{"description": c} if isinstance(c, str) else c for c in claims_raw]
        except Exception:
            claims = []

        # 解析 available_tools（ENABLED_TOOLS 列）
        tools_str = row.get("ENABLED_TOOLS", "[]")
        try:
            available_tools = json.loads(tools_str) if tools_str else []
        except Exception:
            available_tools = []

        should_stop = str(row.get("SHOULD_STOP_EARLY", "False")).lower() == "true"

        # GT 执行日志（TRAJECTORY 列包含 GT 的执行记录）
        gt_log = _safe_str(row.get("TRAJECTORY", ""))

        task = Task(
            task_id=str(row.get("TASK", f"row_{idx}")),
            bucket=bucket,
            hallucination_type=h_type,
            difficulty=difficulty,
            prompt=str(row.get("PROMPT", "")),
            available_tools=available_tools,
            ground_truth={"state_assertions": assertions},
            evaluation_rules=eval_rules,
            claims=claims,
            should_stop_early=should_stop,
            gt_execution_log=gt_log,
            gt_execution_ok=bool(gt_log),
        )
        tasks.append(task)
    return tasks


# ═══════════════════════════════════════════════════════════════════════════════
# 10. 主评测流程（从已有 completion CSV 评分）
# ═══════════════════════════════════════════════════════════════════════════════

def _build_llm_judge_client():
    """
    根据 .env 中的 EVAL_LLM_* 环境变量创建 LLM Judge 客户端。
    若环境变量缺失或 mcp_evals_scores 不可用，返回 None。
    """
    if not _EVALS_AVAILABLE:
        logger.warning("mcp_evals_scores 不可用，LLM Judge 功能已禁用")
        return None

    eval_model = os.getenv("EVAL_LLM_MODEL", "").strip()
    eval_api_key = os.getenv("EVAL_LLM_API_KEY", "").strip() or os.getenv("LLM_API_KEY", "").strip()
    eval_base_url = os.getenv("EVAL_LLM_BASE_URL", "").strip() or os.getenv("LLM_BASE_URL", "").strip()

    if not eval_model:
        logger.warning("EVAL_LLM_MODEL 未配置，LLM Judge 功能已禁用。请在 .env 中设置 EVAL_LLM_MODEL")
        return None
    if not eval_api_key:
        logger.warning("EVAL_LLM_API_KEY 未配置，LLM Judge 功能已禁用")
        return None

    # 临时设置 litellm 环境变量（AsyncLiteLLMClient 在 __init__ 里会读取）
    os.environ["EVAL_LLM_API_KEY"] = eval_api_key
    if eval_base_url:
        os.environ["EVAL_LLM_BASE_URL"] = eval_base_url

    try:
        config = _EvaluatorConfig(evaluator_model=eval_model, semaphore_limit=1)
        client = _AsyncLiteLLMClient(config)
        logger.info(f"✅ LLM Judge 已启用: model={eval_model}, base_url={eval_base_url or '(default)'}")
        return client
    except Exception as e:
        logger.warning(f"LLM Judge 客户端创建失败: {e}，退化为关键词匹配")
        return None


def evaluate_from_completion_csv(
    completion_csv_path: str,
    output_csv_path: str,
    pass_threshold: float = 0.6,
) -> pd.DataFrame:
    """
    从 mcp_completion_script.py 产出的 completion_results CSV 进行幻觉感知评分。

    输入列（来自 mcp_completion_script.py）：
      TASK, PROMPT, TRAJECTORY, GTFA_CLAIMS, ENABLED_TOOLS
      script_model_response, raw_conversation_history, trajectory, errors
      HALLUCINATION_TYPE, BUCKET, DIFFICULTY, EVALUATION_RULES, STATE_ASSERTIONS （可选）

    输出列（新增）：
      hallu_score          - 幻觉感知综合得分 [0, 1]
      hallu_pass           - 是否通过 (score >= threshold)
      hallu_strategy       - 使用的评分策略名称
      hallu_components     - 分项得分 JSON
      agent_step_count     - Agent 实际工具调用步数
      agent_tool_calls     - 工具调用名称列表 JSON

    参数：
      pass_threshold: 通过分数线，默认 0.6
    """
    logger.info(f"加载 completion CSV: {completion_csv_path}")
    df = pd.read_csv(completion_csv_path)
    logger.info(f"共 {len(df)} 条记录")

    # 重建 Task 对象列表
    tasks = build_tasks_from_completion_csv(df)

    # ── 创建 LLM Judge 客户端（读 .env 中的 EVAL_LLM_* 配置）─────────────────
    llm_client = _build_llm_judge_client()

    # 逐行评分
    result_rows = []
    for i, (task, (_, row)) in enumerate(zip(tasks, df.iterrows())):
        logger.info(
            f"[{i+1}/{len(tasks)}] 评分 task={task.task_id} "
            f"type={task.hallucination_type} bucket={task.bucket}"
        )

        # 解析 Agent 轨迹（来自 trajectory 列或 raw_conversation_history 列）
        trajectory_col = row.get("trajectory") or row.get("raw_conversation_history") or ""
        agent_tool_calls = parse_tool_calls_from_trajectory(str(trajectory_col))
        agent_response = parse_model_response(dict(row))
        agent_step_count = len(agent_tool_calls)

        # 执行评分路由（透传 llm_client）
        try:
            components = route_and_score(
                task, agent_tool_calls, agent_response, agent_step_count,
                llm_client=llm_client,
            )
            score = components.get("score", 0.0)
            strategy = components.get("strategy", "unknown")
        except Exception as e:
            logger.error(f"  评分出错: {e}")
            components = {"error": str(e), "score": 0.0}
            score = 0.0
            strategy = "error"

        # 合并原始行 + 新增评分列
        out_row = dict(row)
        out_row["hallu_score"] = score
        out_row["hallu_pass"] = score >= pass_threshold
        out_row["hallu_strategy"] = strategy
        out_row["hallu_components"] = json.dumps(components, ensure_ascii=False)
        out_row["agent_step_count"] = agent_step_count
        out_row["agent_tool_calls"] = json.dumps(agent_tool_calls, ensure_ascii=False)

        result_rows.append(out_row)

    # 构建结果 DataFrame
    result_df = pd.DataFrame(result_rows)
    output_path = Path(output_csv_path)
    output_path.parent.mkdir(parents=True, exist_ok=True)
    result_df.to_csv(output_path, index=False)

    # ── 打印统计报告 ───────────────────────────────────────────────────────────
    _print_eval_report(result_df, pass_threshold)

    logger.info(f"\n✅ 评测结果已保存至: {output_path}")
    return result_df


def _print_eval_report(df: pd.DataFrame, pass_threshold: float):
    """打印简洁的评测统计报告。"""
    print("\n" + "=" * 70)
    print("📊  幻觉感知评测报告")
    print("=" * 70)

    valid = df["hallu_score"].dropna()
    if len(valid) == 0:
        print("  无有效评分数据")
        return

    overall_mean = valid.mean()
    overall_pass = (valid >= pass_threshold).mean()
    print(f"  样本数量    : {len(valid)}")
    print(f"  平均得分    : {overall_mean:.3f}")
    print(f"  通过率(≥{pass_threshold:.2f}): {overall_pass:.1%}")
    print()

    # 按幻觉类型分组
    if "HALLUCINATION_TYPE" in df.columns:
        print("  ─── 按幻觉类型 ───────────────────────────────────")
        for h_type, group in df.groupby("HALLUCINATION_TYPE"):
            scores = group["hallu_score"].dropna()
            if len(scores) == 0:
                continue
            pass_rate = (scores >= pass_threshold).mean()
            print(f"  {h_type:<25} | n={len(scores):3d} | "
                  f"avg={scores.mean():.3f} | pass={pass_rate:.1%}")
        print()

    # 按 Bucket 分组
    if "BUCKET" in df.columns:
        print("  ─── 按 Bucket ─────────────────────────────────────")
        for bucket, group in df.groupby("BUCKET"):
            scores = group["hallu_score"].dropna()
            if len(scores) == 0:
                continue
            pass_rate = (scores >= pass_threshold).mean()
            print(f"  {bucket:<25} | n={len(scores):3d} | "
                  f"avg={scores.mean():.3f} | pass={pass_rate:.1%}")
        print()

    # 按难度分组
    if "DIFFICULTY" in df.columns and df["DIFFICULTY"].nunique() > 1:
        print("  ─── 按难度 ─────────────────────────────────────────")
        for diff, group in df.groupby("DIFFICULTY"):
            scores = group["hallu_score"].dropna()
            if len(scores) == 0:
                continue
            pass_rate = (scores >= pass_threshold).mean()
            print(f"  {diff:<25} | n={len(scores):3d} | "
                  f"avg={scores.mean():.3f} | pass={pass_rate:.1%}")
        print()

    # 策略分布
    if "hallu_strategy" in df.columns:
        print("  ─── 评分策略分布 ───────────────────────────────────")
        for strat, group in df.groupby("hallu_strategy"):
            print(f"  {strat:<35} | n={len(group):3d}")
        print()

    print("=" * 70)


# ═══════════════════════════════════════════════════════════════════════════════
# 11. 完整流程：JSONL → Agent 执行 → 评分
# ═══════════════════════════════════════════════════════════════════════════════

async def run_full_pipeline(
    jsonl_path: str,
    model: str,
    output_csv: str,
    server_url: str,
    concurrency: int = 3,
    num_tasks: Optional[int] = None,
    pass_threshold: float = 0.6,
):
    """
    完整端到端评测流程：
      1. 加载 JSONL 任务
      2. 转换为 CSV（含 ENABLED_TOOLS 等列）
      3. 调用 Agent（复用 mcp_completion_script.py 的 AsyncMCPTrajectoryGenerator）
      4. 幻觉感知评分
      5. 输出结果

    注意：
      需要 mcp-atlas 的 MCP 服务运行中（3000 端口）
    """
    import tempfile

    # 1. 加载任务
    tasks = load_tasks_from_jsonl(jsonl_path)
    if num_tasks:
        tasks = tasks[:num_tasks]

    # 2. 转为 DataFrame（模拟 CSV 结构）
    tmp_csv = tempfile.mktemp(suffix=".csv")
    tasks_df = tasks_to_csv(tasks, tmp_csv)
    logger.info(f"任务 CSV 临时文件: {tmp_csv}")

    # 3. 调用 Agent（复用 mcp_completion_script.py 的逻辑）
    try:
        # 动态导入（需要 mcp-atlas 在 sys.path 中）
        import importlib.util
        mcp_script_path = MCP_ATLAS_EVAL_DIR / "mcp_completion_script.py"

        # mcp_completion_script.py 顶层有 logging.FileHandler("completion_results/mcp_eval.log")
        # 该相对路径基于 mcp_completion_script.py 所在目录，需提前创建并切换工作目录
        _orig_cwd = os.getcwd()
        os.chdir(MCP_ATLAS_EVAL_DIR)
        (MCP_ATLAS_EVAL_DIR / "completion_results").mkdir(exist_ok=True)

        spec = importlib.util.spec_from_file_location("mcp_completion_script", mcp_script_path)
        mcp_mod = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(mcp_mod)

        os.chdir(_orig_cwd)  # 导入完成后切回原目录

        AsyncMCPTrajectoryGenerator = mcp_mod.AsyncMCPTrajectoryGenerator

        completion_csv = output_csv.replace(".csv", "_completion.csv")
        # 每次重跑前删除旧的 completion CSV，避免追加写入导致数据叠加
        Path(completion_csv).unlink(missing_ok=True)
        async with AsyncMCPTrajectoryGenerator(model) as generator:
            # 覆盖 SERVER_URL
            mcp_mod.SERVER_URL = server_url
            await generator.evaluate_dataset_async(
                tasks_df, completion_csv, None, concurrency
            )
        logger.info(f"Agent 执行完毕，结果: {completion_csv}")

    except Exception as e:
        # 确保异常时也切回原目录
        try:
            os.chdir(_orig_cwd)
        except Exception:
            pass
        logger.error(f"Agent 执行失败: {e}，将尝试对空轨迹评分")
        # 若 Agent 执行失败，生成空轨迹的 completion CSV
        empty_rows = []
        for _, row in tasks_df.iterrows():
            r = dict(row)
            r["script_model_response"] = ""
            r["raw_conversation_history"] = "[]"
            r["trajectory"] = "[]"
            r["errors"] = "[]"
            r["trajectory_time"] = 0.0
            r["num_retry"] = 0
            empty_rows.append(r)
        completion_csv = output_csv.replace(".csv", "_completion.csv")
        pd.DataFrame(empty_rows).to_csv(completion_csv, index=False)

    # 4. 幻觉感知评分
    result_df = evaluate_from_completion_csv(completion_csv, output_csv, pass_threshold)

    # 5. 清理临时文件
    try:
        Path(tmp_csv).unlink(missing_ok=True)
    except Exception:
        pass

    return result_df


# ═══════════════════════════════════════════════════════════════════════════════
# 12. CLI 入口
# ═══════════════════════════════════════════════════════════════════════════════

def parse_args():
    parser = argparse.ArgumentParser(
        description="hallu_eval.py — 幻觉类型感知的 MCP Agent 评测框架",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=__doc__,
    )

    # 输入来源（三选一）
    input_group = parser.add_mutually_exclusive_group()
    input_group.add_argument(
        "--input",
        type=str,
        help=(
            "JSONL 任务文件路径。\n"
            "推荐先用 hallu-gt 预跑 GT 日志，再将其输出传给本参数：\n"
            "  Step 1: uv run hallu-gt --input tasks/memory_generated_tasks.jsonl \\\n"
            "                          --output gt/memory_with_gt.jsonl\n"
            "  Step 2: uv run hallu-eval --input gt/memory_with_gt.jsonl ...\n"
            "若直接传入原始 tasks/*.jsonl（无 gt_execution_log），\n"
            "Memory/Reasoning Trap 的语义匹配将退化为关键词匹配。"
        ),
    )
    input_group.add_argument(
        "--from-completion-csv",
        type=str,
        dest="completion_csv",
        help="直接从 mcp_completion_script.py 已生成的 completion CSV 评分（跳过 Agent 执行）",
    )

    parser.add_argument(
        "--convert-only",
        action="store_true",
        help="仅将 JSONL 转换为 CSV，不执行 Agent 也不评分（配合 --input 使用）",
    )
    parser.add_argument(
        "--output",
        type=str,
        required=True,
        help="输出 CSV 文件路径",
    )
    parser.add_argument(
        "--model",
        type=str,
        default=os.getenv("LLM_MODEL", "openai/gpt-4o"),
        help="Agent 模型（LiteLLM 格式，默认: LLM_MODEL 环境变量或 openai/gpt-4o）",
    )
    parser.add_argument(
        "--server-url",
        type=str,
        default=os.getenv("SERVER_URL", "http://localhost:3000"),
        help="mcp-atlas Agent 服务地址（默认: SERVER_URL 环境变量或 http://localhost:3000）",
    )
    parser.add_argument(
        "--concurrency",
        type=int,
        default=3,
        help="并发 Agent 请求数（默认: 3）",
    )
    parser.add_argument(
        "--num-tasks",
        type=int,
        default=None,
        help="限制评测任务数量（调试用）",
    )
    parser.add_argument(
        "--pass-threshold",
        type=float,
        default=0.6,
        help="通过分数线 [0.0-1.0]，默认 0.6",
    )

    return parser.parse_args()


def main():
    args = parse_args()

    # ── 模式 1：仅转换 JSONL → CSV ─────────────────────────────────────────────
    if args.convert_only:
        if not args.input:
            logger.error("--convert-only 需要配合 --input 使用")
            sys.exit(1)
        tasks = load_tasks_from_jsonl(args.input)
        if args.num_tasks:
            tasks = tasks[: args.num_tasks]
        tasks_to_csv(tasks, args.output)
        print(f"\n✅ 转换完成: {args.output}")
        return

    # ── 模式 2：从已有 completion CSV 评分 ────────────────────────────────────
    if args.completion_csv:
        evaluate_from_completion_csv(
            args.completion_csv,
            args.output,
            args.pass_threshold,
        )
        return

    # ── 模式 3：完整流程（JSONL → Agent → 评分）──────────────────────────────
    if not args.input:
        logger.error("请指定 --input（JSONL 文件）或 --from-completion-csv")
        sys.exit(1)

    # ── 预检：Memory/Reasoning Trap 需要先跑 hallu-gt ──────────────────────────
    _check_tasks = load_tasks_from_jsonl(args.input)
    if args.num_tasks:
        _check_tasks = _check_tasks[: args.num_tasks]
    _need_gt = [
        t for t in _check_tasks
        if t.hallucination_type in (HallucinationType.MEMORY, HallucinationType.REASONING)
        and not t.gt_execution_ok
    ]
    if _need_gt:
        print(
            f"\n⚠️  警告：{len(_need_gt)} 条 Memory/Reasoning Trap 任务缺少 gt_execution_log，"
            f"语义匹配将退化为关键词匹配（精度较低）。\n"
            f"建议先运行：\n"
            f"  uv run hallu-gt --input {args.input} \\\n"
            f"                  --output <带GT日志的输出.jsonl>\n"
            f"然后将输出文件传给 --input。"
        )

    asyncio.run(
        run_full_pipeline(
            jsonl_path=args.input,
            model=args.model,
            output_csv=args.output,
            server_url=args.server_url,
            concurrency=args.concurrency,
            num_tasks=args.num_tasks,
            pass_threshold=args.pass_threshold,
        )
    )


if __name__ == "__main__":
    main()

"""
阶段 1 ── run_gt_execution.py（串行，有网络 I/O）
  for task in tasks:
      调用 MCP Server → 等待结果 → 写入 gt_execution_log
  全部完成后 → 写入 *_with_gt.jsonl

阶段 2 ── mcp_completion_script.py（有并发，有网络 I/O）
  async with semaphore(concurrency=3):
      并发调用 Agent → 等待轨迹

阶段 3 ── hallu_eval.py 评分（串行，纯本地计算）
  for row in completion_csv:
      route_and_score()  ← 无 I/O，不需要并发
"""