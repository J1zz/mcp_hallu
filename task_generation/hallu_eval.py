"""hallu_eval.py 
推荐流程：
  # Step 1：预跑 GT（Memory / Reasoning Trap 需要）
  uv run hallu-gt  --input tasks/reasoning_generated_tasks.jsonl \\
                   --output gt/reasoning_with_gt.jsonl

  # 评测
  uv run hallu-eval --input tasks/reasoning_generated_tasks.jsonl \\
                    --model openai/gpt-4o --output results/eval.csv

  # 跳过 Agent 执行，对已有 completion CSV 评分
  uv run hallu-eval --from-completion-csv completion_results/sample.csv \\
                    --output results/eval.csv
"""

import argparse
import asyncio
import logging
import os
import sys

from eval import (
    HallucinationType,
    evaluate_from_completion_csv,
    load_tasks_from_jsonl,
    run_full_pipeline,
    tasks_to_csv,
)

logger = logging.getLogger(__name__)


def _parse_args():
    p = argparse.ArgumentParser(
        description="hallu_eval.py — 幻觉类型感知的 MCP Agent 评测框架",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=__doc__,
    )
    src = p.add_mutually_exclusive_group()
    src.add_argument("--input", help="JSONL 任务文件")
    src.add_argument("--from-completion-csv", dest="completion_csv", help="直接对已有 completion评分")
    p.add_argument("--convert-only", action="store_true", help="仅将 JSONL 转为 CSV，不执行 Agent 也不评分（需配合 --input）")
    p.add_argument("--output", equired=True,  help="输出 CSV 文件路径")
    p.add_argument("--model", default=os.getenv("LLM_MODEL", "openai/gpt-4o"))
    p.add_argument("--server-url",     default=os.getenv("SERVER_URL", "http://localhost:3000"),help="mcp-atlas Agent 服务地址")
    p.add_argument("--concurrency",    type=int,   default=3,   help="并发 Agent 请求数")
    p.add_argument("--num-tasks",      type=int,   default=None, help="限制任务数量（调试用）")
    p.add_argument("--pass-threshold", type=float, default=0.6, help="通过分数线 [0.0-1.0]")
    return p.parse_args()


def main():
    args = _parse_args()

    # 仅转换 JSONL → CSV
    if args.convert_only:
        if not args.input:
            logger.error("--convert-only 需要配合 --input 使用")
            sys.exit(1)
        tasks = load_tasks_from_jsonl(args.input)
        if args.num_tasks:
            tasks = tasks[:args.num_tasks]
        tasks_to_csv(tasks, args.output)
        print(f"\n转换完成: {args.output}")
        return

    if args.completion_csv:
        evaluate_from_completion_csv(args.completion_csv, args.output, args.pass_threshold)
        return

    # 完整流程
    if not args.input:
        logger.error("请指定 --input 或 --from-completion-csv")
        sys.exit(1)

    check = load_tasks_from_jsonl(args.input)
    if args.num_tasks:
        check = check[:args.num_tasks]
    missing = [
        t for t in check
        if t.hallucination_type in (HallucinationType.MEMORY, HallucinationType.REASONING)
        and not t.gt_execution_ok
    ]
    if missing:
        print(
            f"\n⚠  {len(missing)} 条 Memory/Reasoning Trap 任务缺少 gt_execution_log，"
            f"语义匹配将退化为关键词匹配。\n"
            f"建议先运行：uv run hallu-gt --input {args.input} --output <with_gt.jsonl>"
        )

    asyncio.run(run_full_pipeline(
        jsonl_path=args.input,
        model=args.model,
        output_csv=args.output,
        server_url=args.server_url,
        concurrency=args.concurrency,
        num_tasks=args.num_tasks,
        pass_threshold=args.pass_threshold,
    ))


if __name__ == "__main__":
    main()
