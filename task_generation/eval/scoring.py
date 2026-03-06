"""评分策略：四种幻觉类型的评分函数 + 路由器。"""

import asyncio
import concurrent.futures
import json
import logging
from typing import Any, Dict, List, Optional

from .schema import HallucinationType, STATEFUL_BUCKETS, Task
from .trajectory import _safe_str

logger = logging.getLogger(__name__)

def _tool_hit_rate(required: List[str], tool_set: set) -> float:
    return sum(1 for t in required if t in tool_set) / max(1, len(required)) if required else 1.0


def _keyword_semantic_match(task: Task, agent_response: str) -> float:
    """基于 claims 关键词的轻量语义匹配，无信号时返回 0.5。"""
    keywords = []
    for c in task.claims:
        if isinstance(c, dict):
            words = [w.strip(".,;:'\"()") for w in c.get("description", "").split() if len(w) > 3]
            keywords.extend(words[:5])
    if not keywords or not agent_response:
        return 0.5
    resp = agent_response.lower()
    return min(1.0, sum(1 for kw in keywords if kw.lower() in resp) / max(1, len(keywords)))


def _call_llm_judge(
    task: Task,
    agent_tool_calls: List[str],
    agent_response: str,
    gt_log: str,
    llm_client: Any,
) -> Optional[float]:
    """调用 LLM Judge 对 agent 执行结果语义评分（0.0–1.0）；失败返回 None。"""
    import litellm as _ll

    model  = llm_client.config.evaluator_model
    prompt = (
        "You are an evaluator comparing an AI agent's execution against a ground-truth execution log.\n\n"
        f"=== Ground Truth Execution Log ===\n{gt_log}\n\n"
        f"=== Agent Response ===\n{agent_response or '(empty)'}\n\n"
        f"=== Agent Tool Calls ===\n{json.dumps(agent_tool_calls)}\n\n"
        f"Task: {task.prompt}\n\n"
        "Rate 0.0–1.0 how well the agent's execution matches the ground truth.\n"
        "Consider: correct tools, correct order, correct parameters, correct final answer.\n"
        'Respond ONLY with JSON: {"score": <float>, "reason": "<brief>"}\n'
    )
    resp_fmt = (
        {"type": "json_object", "response_schema": {
            "type": "object",
            "properties": {"score": {"type": "number"}, "reason": {"type": "string"}},
            "required": ["score"],
        }} if "gemini" in model.lower()
        else {"type": "json_object"}
    )

    async def _acall():
        resp = await _ll.acompletion(
            model=model,
            messages=[{"role": "user", "content": prompt}],
            response_format=resp_fmt,
            temperature=1 if "gpt-5" in model else 0.0,
            api_key=_ll.api_key,
            api_base=getattr(_ll, "api_base", None) or None,
        )
        return json.loads(resp.choices[0].message.content or "{}")

    try:
        loop = asyncio.get_event_loop()
        if loop.is_running():
            with concurrent.futures.ThreadPoolExecutor() as pool:
                result = pool.submit(asyncio.run, _acall()).result(timeout=60)
        else:
            result = loop.run_until_complete(_acall())
    except RuntimeError:
        result = asyncio.run(_acall())

    score = float(max(0.0, min(1.0, result.get("score", 0.5))))
    logger.info(f"  LLM Judge: {score:.3f} | {result.get('reason', '')[:80]}")
    return score

# 评分策略
def score_confusion_trap(
    task: Task,
    agent_tool_calls: List[str],
    agent_response: str,
) -> Dict[str, Any]:
    """Confusion Trap：required 命中率 + forbidden 惩罚 + 首选正确奖励。

    score = required_hit × (1 − forbidden_penalty × 0.5) × (1.1 if first_correct)
    """
    rules        = task.evaluation_rules
    required     = rules.get("required_tools", [])
    forbidden    = rules.get("forbidden_tools", [])
    correct_tool = rules.get("correct_tool")
    tool_set     = set(agent_tool_calls)

    required_hit      = _tool_hit_rate(required, tool_set)
    forbidden_penalty = _tool_hit_rate(forbidden, tool_set) if forbidden else 0.0
    correct_first     = bool(correct_tool and agent_tool_calls and agent_tool_calls[0] == correct_tool)

    raw = required_hit * (1.0 - forbidden_penalty * 0.5)
    if correct_first:
        raw = min(1.0, raw * 1.1)

    return {
        "strategy":             "confusion_trap",
        "required_tools":       required,
        "forbidden_tools":      forbidden,
        "agent_used_required":  [t for t in required if t in tool_set],
        "agent_used_forbidden": [t for t in forbidden if t in tool_set],
        "required_hit":         round(required_hit, 4),
        "forbidden_penalty":    round(forbidden_penalty, 4),
        "correct_tool_first":   correct_first,
        "score":                round(max(0.0, min(1.0, raw)), 4),
    }


def score_void_trap(
    task: Task,
    agent_tool_calls: List[str],
    agent_response: str,
    max_allowed_steps: int = 5,
) -> Dict[str, Any]:
    """Void Trap：任务无解，Agent 越早识别并放弃得分越高。"""
    QUIT_KEYWORDS = [
        "cannot", "can't", "unable to", "not possible", "no tool",
        "not available", "unsolvable", "无法", "不能", "没有工具", "不支持",
        "does not exist", "not exist", "no such tool",
    ]
    steps      = len(agent_tool_calls)
    quit_found = any(kw in agent_response.lower() for kw in QUIT_KEYWORDS)

    if not quit_found:
        return {"strategy": "void_trap", "tool_calls_count": steps,
                "base_score": 0.0, "additional_score": 0.0,
                "max_allowed_steps": max_allowed_steps, "score": 0.0}

    additional = (
        0.5 if steps == 0
        else max(0.0, 0.5 - steps * 0.1) if steps <= max_allowed_steps
        else 0.0
    )
    return {
        "strategy":          "void_trap",
        "tool_calls_count":  steps,
        "base_score":        0.5,
        "additional_score":  additional,
        "max_allowed_steps": max_allowed_steps,
        "score":             round(0.5 + additional, 4),
    }


def score_parallel_execution(
    task: Task,
    agent_tool_calls: List[str],
    agent_response: str,
    llm_client: Any = None,
) -> Dict[str, Any]:
    """Memory / Reasoning Trap（无状态 bucket）：对比 agent 轨迹与 GT 执行日志。

    score = tool_coverage×0.4 + semantic×(0.6−branch_w) + branch_score×branch_w
    """
    required  = task.evaluation_rules.get("required_tools", [])
    tool_set  = set(agent_tool_calls)
    gt_log    = _safe_str(task.gt_execution_log)

    tool_coverage = _tool_hit_rate(required, tool_set)

    semantic, method = None, "keyword_fallback"
    if gt_log and llm_client is not None:
        try:
            semantic = _call_llm_judge(task, agent_tool_calls, agent_response, gt_log, llm_client)
            method   = "llm_judge"
        except Exception as e:
            logger.warning(f"  LLM Judge 失败，退化到关键词匹配: {e}")
    if semantic is None:
        semantic = _keyword_semantic_match(task, agent_response)
        method   = "keyword_fallback"

    branch_correct = None
    if task.hallucination_type == HallucinationType.REASONING:
        gl = gt_log.lower()
        triggered = (
            "A" if ("branch a triggered" in gl or "branch_a" in gl) else
            "B" if ("branch b triggered" in gl or "branch_b" in gl) else None
        )
        if triggered:
            rl = (agent_response or "").lower()
            branch_correct = (
                ("branch a" in rl or "more than 5" in rl or ">5" in rl) if triggered == "A"
                else ("branch b" in rl or "5 or fewer" in rl or "expanded" in rl)
            )

    branch_w     = 0.1 if task.hallucination_type == HallucinationType.REASONING else 0.0
    branch_score = 1.0 if branch_correct else (0.5 if branch_correct is None else 0.0)
    raw = tool_coverage * 0.4 + semantic * (0.6 - branch_w) + branch_score * branch_w

    gl = gt_log.lower()
    return {
        "strategy":               "parallel_execution",
        "required_tools":         required,
        "agent_used_required":    [t for t in required if t in tool_set],
        "tool_coverage":          round(tool_coverage, 4),
        "semantic_match":         round(semantic, 4),
        "semantic_match_method":  method,
        "branch_triggered_in_gt": (
            "A" if "branch a triggered" in gl else
            "B" if "branch b triggered" in gl else "unknown"
        ),
        "agent_branch_correct":   branch_correct,
        "score":                  round(max(0.0, min(1.0, raw)), 4),
    }


def score_state_assertions(
    task: Task,
    agent_tool_calls: List[str],
    agent_response: str,
) -> Dict[str, Any]:
    """Memory / Reasoning Trap（有状态 bucket）：执行 GT 断言表达式验证世界状态。

    断言全部失败或为空时，退化为工具覆盖率 + 关键词匹配。
    """
    import os as _os, re as _re

    assertions = task.ground_truth.get("state_assertions", [])
    required   = task.evaluation_rules.get("required_tools", [])
    tool_set   = set(agent_tool_calls)
    results    = []

    for a in assertions:
        if isinstance(a, dict):
            expr     = a.get("code") or a.get("assertion") or ""
            expected = a.get("expected", True)
            desc     = a.get("description", expr)
        elif isinstance(a, str):
            expr, expected, desc = a, True, a
        else:
            continue
        if not expr:
            continue
        try:
            actual = eval(expr, {"os": _os, "json": json, "re": _re, "__builtins__": {}})
            passed = bool(actual) == bool(expected)
        except Exception as e:
            actual, passed, desc = None, False, f"{desc} [exec_error: {e}]"
        results.append({"description": desc, "expression": expr,
                        "expected": expected, "actual": str(actual), "passed": passed})

    if results:
        passed_n = sum(1 for r in results if r["passed"])
        return {
            "strategy":          "state_assertion",
            "assertions_total":  len(results),
            "assertions_passed": passed_n,
            "assertion_details": results,
            "fallback_used":     False,
            "score":             round(passed_n / len(results), 4),
        }

    cov = _tool_hit_rate(required, tool_set)
    kw  = _keyword_semantic_match(task, agent_response)
    return {
        "strategy":            "state_assertion_fallback",
        "required_tools":      required,
        "agent_used_required": [t for t in required if t in tool_set],
        "tool_coverage":       round(cov, 4),
        "keyword_score":       round(kw, 4),
        "fallback_used":       True,
        "fallback_reason":     "no state_assertions or all exec_error",
        "score":               round(cov * 0.6 + kw * 0.4, 4),
    }


def route_and_score(
    task: Task,
    agent_tool_calls: List[str],
    agent_response: str,
    agent_step_count: int,
    llm_client: Any = None,
) -> Dict[str, Any]:
    """根据幻觉类型 + Bucket 路由到对应评分策略。"""
    if task.hallucination_type == HallucinationType.CONFUSION:
        return score_confusion_trap(task, agent_tool_calls, agent_response)
    if task.hallucination_type == HallucinationType.VOID:
        return score_void_trap(task, agent_tool_calls, agent_response)
    if task.bucket.upper() in STATEFUL_BUCKETS:
        return score_state_assertions(task, agent_tool_calls, agent_response)
    return score_parallel_execution(task, agent_tool_calls, agent_response, llm_client=llm_client)
