"""hallu_eval.py

完整评测流程（无需预跑 GT）：

  uv run hallu-eval --input new_task/confusion_tasks.jsonl \\
                    --model openai/gpt-4o-2024-05-13 \\
                    --output results/confusion_eval.csv

流程说明
--------
1. 加载 JSONL 任务（支持 Confusion / Void / Memory / Reasoning 四种类型）
2. 启动待测模型 Agent，并发执行所有任务（--concurrency 控制并发数）
3. 每条任务 Agent 执行完毕后，立即：
   a. 若任务类型为 Memory/Reasoning Trap 且 strategy=dynamic_script，
      自动执行 GT 参考脚本（run_one_gt_script），生成 gt_execution_log
      （与 LLM-as-judge 使用同一个 eval 模型配置）
   b. 调用 route_and_score 打分
   c. 写出单任务 JSON 结果文件（默认位于 results/task_results/<task_id>.json）
   d. 追加到汇总 CSV
4. 所有任务完成后打印汇总报告

跳过 Agent 执行，对已有 completion CSV 直接打分：

  uv run hallu-eval --from-completion-csv results/confusion_eval_completion.csv \\
                    --input new_task/confusion_tasks.jsonl \\
                    --output results/confusion_eval.csv

仅将 JSONL 转为中间 CSV（调试用）：

  uv run hallu-eval --input new_task/confusion_tasks.jsonl \\
                    --convert-only \\
                    --output /tmp/tasks.csv
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
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=__doc__,
    )
    src = p.add_mutually_exclusive_group()
    src.add_argument("--input", help="JSONL 任务文件（完整流程或搭配 --from-completion-csv 使用）")
    src.add_argument("--from-completion-csv", dest="completion_csv", help="直接对已有 completion CSV 打分（跳过 Agent 执行）")
    p.add_argument("--convert-only", action="store_true", help="仅将 JSONL 转为中间 CSV，不执行 Agent 也不评分（需配合 --input）")
    p.add_argument("--output", required=True, help="输出 CSV 文件路径")
    p.add_argument("--model", default=os.getenv("LLM_MODEL", "openai/gpt-4o"))
    p.add_argument("--server-url", default=os.getenv("SERVER_URL", "http://localhost:3000"), help="mcp-atlas Agent 服务地址")
    p.add_argument("--concurrency", type=int, default=3, help="并发 Agent 请求数")
    p.add_argument("--num-tasks", type=int, default=None, help="限制任务数量（调试用）")
    p.add_argument("--pass-threshold", type=float, default=0.6, help="通过分数线 [0.0-1.0]")
    p.add_argument(
        "--json-output-dir",
        default=None,
        help="单任务 JSON 结果输出目录（默认：output CSV 同目录下的 task_results/）",
    )
    # --input 可与 --from-completion-csv 搭配使用（为 dynamic_script 任务提供 GT 脚本）
    # 当 --from-completion-csv 单独使用时，--input 可选
    p.add_argument(
        "--jsonl-for-gt",
        default=None,
        dest="jsonl_for_gt",
        help="搭配 --from-completion-csv 使用：指定原始 JSONL 文件，为 dynamic_script 任务提供 GT 脚本（默认从 --input 取）",
    )
    p.add_argument(
        "--docker-snapshot",
        action="store_true",
        dest="docker_snapshot",
        help=(
            "开启 Docker /data 快照隔离模式：在每条任务执行前自动恢复容器 /data 到初始状态，"
            "彻底消除任务间状态污染。此模式下并发数自动降为 1（串行执行）。"
            "快照/恢复各耗时约 1s，476 条任务总额外开销约 8 分钟。"
        ),
    )
    return p.parse_args()


def main():
    args = _parse_args()

    # ── 仅转换 JSONL → CSV ────────────────────────────────────────────────────
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

    # ── 从已有 completion CSV 打分 ────────────────────────────────────────────
    if args.completion_csv:
        # jsonl_for_gt 优先；其次尝试 --input；都没有则 None
        jsonl_path = args.jsonl_for_gt or args.input or None
        evaluate_from_completion_csv(
            completion_csv_path=args.completion_csv,
            output_csv_path=args.output,
            pass_threshold=args.pass_threshold,
            jsonl_path=jsonl_path,
            json_output_dir=args.json_output_dir,
        )
        return

    # ── 完整流程：Agent 执行 + GT 生成 + 打分 ────────────────────────────────
    if not args.input:
        logger.error("请指定 --input 或 --from-completion-csv")
        sys.exit(1)

    all_tasks = load_tasks_from_jsonl(args.input)
    if args.num_tasks:
        all_tasks = all_tasks[:args.num_tasks]

    # 提示：dynamic_script 任务将在运行时自动生成 GT，无需预跑
    dynamic_script_tasks = [
        t for t in all_tasks
        if t.hallucination_type in (HallucinationType.MEMORY, HallucinationType.REASONING)
        and t.ground_truth.get("strategy") == "dynamic_script"
        and not t.gt_execution_ok
    ]
    if dynamic_script_tasks:
        print(
            f"\n📋 {len(dynamic_script_tasks)} 条 dynamic_script 任务将在 Agent 执行完成后"
            f" 自动生成 GT 执行日志。\n"
        )

    asyncio.run(run_full_pipeline(
        jsonl_path=args.input,
        model=args.model,
        output_csv=args.output,
        server_url=args.server_url,
        concurrency=args.concurrency,
        num_tasks=args.num_tasks,
        pass_threshold=args.pass_threshold,
        json_output_dir=args.json_output_dir,
        use_docker_snapshot=args.docker_snapshot,
    ))


if __name__ == "__main__":
    main()
