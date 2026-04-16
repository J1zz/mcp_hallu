import argparse
import json
import os
import re
import time
from pathlib import Path
from itertools import product
from openai import OpenAI
from openai import RateLimitError, APIStatusError, APIConnectionError

"""# 生成 Memory Trap（ANALYTICS + FINANCIAL，全难度）
uv run python generate_tasks_from_prompt.py \
  --type memory \
  --output tasks/memory_v2_tasks.jsonl

# 只生成某个 bucket 和某个难度
uv run python generate_tasks_from_prompt.py \
  --type memory \
  --output tasks/memory_analytics_easy.jsonl \
  --buckets ANALYTICS \
  --difficulties Easy

# 生成其他类型（reasoning / confusion / void）
uv run python generate_tasks_from_prompt.py \
  --type void \
  --output tasks/void_tasks.jsonl

# 生成全部 4 种类型 × 全部 bucket
uv run python generate_tasks_from_prompt.py \
  --output tasks/all_v2_tasks.jsonl"""
# 脚本所在目录作为根目录（兼容任意工作目录）
ROOT = Path(__file__).parent
TOOLS_FILE_STATELESS = ROOT / "tools_discription/ list-tools_bucketed_readonly.json"
TOOLS_FILE_STATEFUL  = ROOT / "tools_discription/list-tools_bucketed.json"
PROMPTS_DIR = ROOT / "prompts"

# Prompt 文件映射（无状态：dynamic_script）
PROMPT_FILES = {
    "Reasoning Trap": PROMPTS_DIR / "reasoning_trap_prompt.md",
    "Void Trap":      PROMPTS_DIR / "void_trap_prompt.md",
    "Confusion Trap": PROMPTS_DIR / "confusion_trap_prompt.md",
    "Memory Trap":    PROMPTS_DIR / "memory_trap_prompt.md",
}

# Prompt 文件映射（有状态：state_check）
PROMPT_FILES_STATEFUL = {
    "Reasoning Trap": PROMPTS_DIR / "reasoning_trap_stateful_prompt.md",
    "Memory Trap":    PROMPTS_DIR / "memory_trap_stateful_prompt.md",
    "Confusion Trap": PROMPTS_DIR / "confusion_trap_stateful_prompt.md",
}

# 类型名简写 → 全称，方便命令行传参
TYPE_ALIASES = {
    "memory": "Memory Trap",
    "reasoning": "Reasoning Trap",
    "confusion": "Confusion Trap",
    "void": "Void Trap",
    "Memory Trap": "Memory Trap",
    "Reasoning Trap": "Reasoning Trap",
    "Confusion Trap": "Confusion Trap",
    "Void Trap": "Void Trap",
}
# LLM 配置（优先读环境变量）
LLM_MODEL = os.getenv("LLM_MODEL", "gemini-3-pro-preview")
LLM_API_KEY = os.getenv("LLM_API_KEY", "2004053110662512723")
LLM_BASE_URL = os.getenv("LLM_BASE_URL", "https://aigc.sankuai.com/v1/openai/native")

# 限速配置（优先读环境变量）
# RPM_DELAY：每次请求后强制等待的秒数，用于主动限速（如 RPM=10 则设为 6）
# MAX_RETRIES：遇到 429/5xx 时最大重试次数
# RETRY_BASE_WAIT：指数退避的初始等待秒数（每次翻倍）
RPM_DELAY = float(os.getenv("RPM_DELAY", "0"))       # 0 = 不主动限速
MAX_RETRIES = int(os.getenv("MAX_RETRIES", "5"))
RETRY_BASE_WAIT = float(os.getenv("RETRY_BASE_WAIT", "10"))

def load_tools_by_bucket(tools_file: Path):
    tools = json.loads(tools_file.read_text())
    by_bucket = {}
    for tool in tools:
        bucket = tool.get("bucket", "UNMAPPED")
        by_bucket.setdefault(bucket, []).append(tool)
    return by_bucket


def _validate_stateful(task: dict) -> bool:
    """校验 stateful 任务结构：
    - strategy == state_check
    - state_assertions 非空
    - dynamic_reference_script 为空
    - 每条 assertion 的 code 语法合法且包含 'result =' 赋值
    """
    gt = task.get("ground_truth", {})
    if gt.get("strategy") != "state_check":
        return False
    assertions = gt.get("state_assertions")
    if not assertions:
        return False
    if gt.get("dynamic_reference_script", "").strip():
        return False
    for a in assertions:
        if not isinstance(a, dict):
            return False
        code = (a.get("code") or "").strip()
        if not code:
            return False
        try:
            compile(code, "<assertion>", "exec")
        except SyntaxError:
            return False
        if "result" not in code:
            return False
    return True


def build_prompt(template: str, hallucination_type: str, bucket: str, difficulty: str, tools):
    tool_descriptions = json.dumps(tools, ensure_ascii=False, indent=2)
    prompt = (
        template.replace("{{hallucination_type}}", hallucination_type)
        .replace("{{target_bucket}}", bucket)
        .replace("{{difficulty}}", difficulty)
        .replace("{{tool_descriptions}}", tool_descriptions)
    )
    return prompt


def extract_json_from_response(text: str) -> dict | None:
    """从模型响应中提取 JSON 对象（兼容 Gemini 多行代码块输出）"""
    if not text or not text.strip():
        return None
    text = text.strip()

    try:
        return json.loads(text)
    except json.JSONDecodeError:
        pass

    json_pattern = r"```(?:json)?\s*(\{.*\})\s*```"
    matches = re.findall(json_pattern, text, re.DOTALL)
    if matches:
        # 取最长的匹配（最可能是完整 JSON）
        for match in sorted(matches, key=len, reverse=True):
            try:
                return json.loads(match)
            except json.JSONDecodeError:
                continue

    start = text.find("{")
    if start != -1:
        depth = 0
        in_string = False
        escape_next = False
        for i, ch in enumerate(text[start:], start):
            if escape_next:
                escape_next = False
                continue
            if ch == "\\" and in_string:
                escape_next = True
                continue
            if ch == '"':
                in_string = not in_string
                continue
            if in_string:
                continue
            if ch == "{":
                depth += 1
            elif ch == "}":
                depth -= 1
                if depth == 0:
                    try:
                        return json.loads(text[start : i + 1])
                    except json.JSONDecodeError:
                        break  # 找到了括号配对但解析失败，不再尝试

    return None


def generate_task_with_llm(prompt: str, client: OpenAI) -> dict | None:
    """
    使用 LLM 生成 task，带指数退避重试 + 主动限速等待。

    重试策略：
      - RateLimitError (429)：服务端限速，指数退避等待后重试
      - APIStatusError 5xx  ：服务端临时错误，指数退避后重试
      - APIConnectionError  ：网络抖动，短暂等待后重试
      - 其他异常            ：直接失败，不重试

    主动限速：
      - RPM_DELAY > 0 时，每次成功请求后等待 RPM_DELAY 秒
      - 例如 API 限制 10 RPM，设 RPM_DELAY=6 可保持在限额内
    """
    # 根据模型类型设置不同的额外参数
    _is_openai_model = LLM_MODEL.startswith(("gpt-", "o1", "o3", "o4"))
    _is_gemini_model = LLM_MODEL.startswith("gemini-")
    if _is_openai_model:
        # OpenAI：强制 JSON 输出
        _extra_kwargs: dict = {"response_format": {"type": "json_object"}}
    elif _is_gemini_model:
        # Gemini 2.5 系列：正常思考，但不把思考内容输出到 content（不占用 max_tokens）
        _extra_kwargs = {"extra_body": {"google": {"thinking_config": {"include_thoughts": False}}}}
    else:
        _extra_kwargs = {}

    for attempt in range(1, MAX_RETRIES + 1):
        try:
            response = client.chat.completions.create(
                model=LLM_MODEL,
                messages=[
                    {
                        "role": "system",
                        "content": "You are a task generation assistant. Always respond with valid JSON only, no additional text.",
                    },
                    {"role": "user", "content": prompt},
                ],
                temperature=0.7,
                max_tokens=18192,
                **_extra_kwargs,
            )

            choice = response.choices[0]
            finish_reason = choice.finish_reason
            if finish_reason == "length":
                print(f"  Warning: Response truncated (finish_reason=length), increase max_tokens")
                return None

            content = choice.message.content
            if not content:
                print("  Warning: Empty response from LLM")
                return None

            task = extract_json_from_response(content)
            if task is None:
                print(f"  Warning: Failed to parse JSON: {content[:200]}...")
                return None

            # 成功后主动限速等待
            if RPM_DELAY > 0:
                time.sleep(RPM_DELAY)

            return task

        except RateLimitError as e:
            # 429：优先读服务端返回的 Retry-After，否则指数退避
            retry_after = getattr(e, "retry_after", None)
            wait = float(retry_after) if retry_after else RETRY_BASE_WAIT * (2 ** (attempt - 1))
            if attempt >= MAX_RETRIES:
                print(f"  RateLimitError: 已达最大重试次数 ({MAX_RETRIES})，放弃")
                return None
            print(f"  RateLimitError (429)，{wait:.0f}s 后重试 [{attempt}/{MAX_RETRIES}]...")
            time.sleep(wait)

        except APIStatusError as e:
            # 5xx 服务端错误
            if e.status_code < 500:
                # 4xx（除 429）不重试
                print(f"  APIStatusError {e.status_code}: {e.message}，不重试")
                return None
            wait = RETRY_BASE_WAIT * (2 ** (attempt - 1))
            if attempt >= MAX_RETRIES:
                print(f"  APIStatusError {e.status_code}: 已达最大重试次数，放弃")
                return None
            print(f"  APIStatusError {e.status_code}，{wait:.0f}s 后重试 [{attempt}/{MAX_RETRIES}]...")
            time.sleep(wait)

        except APIConnectionError as e:
            wait = RETRY_BASE_WAIT * (2 ** (attempt - 1))
            if attempt >= MAX_RETRIES:
                print(f"  APIConnectionError: 已达最大重试次数，放弃")
                return None
            print(f"  APIConnectionError，{wait:.0f}s 后重试 [{attempt}/{MAX_RETRIES}]...")
            time.sleep(wait)

        except Exception as e:
            print(f"  Error calling LLM: {e}")
            return None

    return None


def load_prompt_template(hallucination_type: str, stateful: bool = False) -> str:
    """根据幻觉类型和模式加载对应的 prompt 模板。"""
    prompt_files = PROMPT_FILES_STATEFUL if stateful else PROMPT_FILES
    prompt_file = prompt_files.get(hallucination_type)
    if not prompt_file or not prompt_file.exists():
        raise FileNotFoundError(
            f"Prompt file not found for {hallucination_type} (stateful={stateful}): {prompt_file}"
        )
    return prompt_file.read_text(encoding="utf-8")


def main():
    parser = argparse.ArgumentParser(
        description="使用指定幻觉类型 Prompt 生成 benchmark 任务",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""
示例用法：
  # 生成 Memory Trap 全部 bucket × 难度组合（无状态）
  uv run python generate_tasks_from_prompt.py --type memory --output tasks/memory_v2.jsonl

  # 生成有状态 Reasoning Trap（任意 bucket）
  uv run python generate_tasks_from_prompt.py --type reasoning --stateful \\
      --output tasks/reasoning_stateful.jsonl

  # 只生成 ANALYTICS bucket 的 Easy/Medium（无状态）
  uv run python generate_tasks_from_prompt.py --type memory --output tasks/memory_v2.jsonl \\
      --buckets ANALYTICS --difficulties Easy Medium

  # 生成所有无状态类型（不指定 --type 则生成全部）
  uv run python generate_tasks_from_prompt.py --output tasks/all_tasks.jsonl
        """,
    )
    parser.add_argument(
        "--type",
        dest="hallu_type",
        default=None,
        help="幻觉类型（memory/reasoning/confusion/void），不指定则生成所有类型",
    )
    parser.add_argument(
        "--output",
        required=True,
        help="输出文件路径（相对 task_generation/ 目录或绝对路径），如 tasks/memory_v2.jsonl",
    )
    parser.add_argument(
        "--buckets",
        nargs="+",
        default=None,
        help="只生成指定 bucket 的任务，如 --buckets ANALYTICS FINANCIAL（默认全部 bucket）",
    )
    parser.add_argument(
        "--difficulties",
        nargs="+",
        default=["Easy", "Medium", "Hard"],
        help="难度列表（默认 Easy Medium Hard）",
    )
    parser.add_argument(
        "--stateful",
        action="store_true",
        help="生成 state_check 类型任务（agent 需写入文件状态）。仅支持 Reasoning Trap / Memory Trap。",
    )
    args = parser.parse_args()

    if not LLM_API_KEY:
        print("Error: LLM_API_KEY not set (env var or default)")
        return

    # 解析幻觉类型
    if args.hallu_type:
        resolved = TYPE_ALIASES.get(args.hallu_type)
        if not resolved:
            print(f"Error: unknown type '{args.hallu_type}'. Choose from: memory, reasoning, confusion, void")
            return
        # stateful 模式仅支持 Reasoning/Memory/Confusion Trap
        if args.stateful and resolved not in PROMPT_FILES_STATEFUL:
            print(f"Error: --stateful 不支持 '{resolved}'，仅支持 Reasoning Trap / Memory Trap / Confusion Trap")
            return
        hallucination_types = [resolved]
    else:
        if args.stateful:
            hallucination_types = list(PROMPT_FILES_STATEFUL.keys())
        else:
            hallucination_types = list(PROMPT_FILES.keys())

    # 验证 prompt 文件存在
    prompt_map = PROMPT_FILES_STATEFUL if args.stateful else PROMPT_FILES
    for ht in hallucination_types:
        pf = prompt_map[ht]
        if not pf.exists():
            print(f"Error: Prompt file not found: {pf}")
            return

    # 输出路径
    output_path = Path(args.output)
    if not output_path.is_absolute():
        output_path = ROOT / args.output
    output_path.parent.mkdir(parents=True, exist_ok=True)

    # 初始化 LLM 客户端
    client_kwargs = {"api_key": LLM_API_KEY}
    if LLM_BASE_URL:
        client_kwargs["base_url"] = LLM_BASE_URL
    client = OpenAI(**client_kwargs)

    # stateful 模式使用完整工具集（含写操作），stateless 使用只读工具集
    tools_file = TOOLS_FILE_STATEFUL if args.stateful else TOOLS_FILE_STATELESS
    tools_by_bucket = load_tools_by_bucket(tools_file)
    print(f"  Tools file : {tools_file.name}  ({sum(len(v) for v in tools_by_bucket.values())} tools)")

    # 过滤 bucket
    if args.buckets:
        unknown = [b for b in args.buckets if b not in tools_by_bucket]
        if unknown:
            print(f"Warning: unknown buckets {unknown}, available: {list(tools_by_bucket.keys())}")
        buckets = [b for b in args.buckets if b in tools_by_bucket]
    else:
        buckets = list(tools_by_bucket.keys())

    difficulties = args.difficulties

    combos = list(product(buckets, hallucination_types, difficulties))
    print(f"Total combinations to generate: {len(combos)}")
    print(f"  Types      : {hallucination_types}")
    print(f"  Buckets    : {buckets}")
    print(f"  Difficulties: {difficulties}")
    print(f"  Model      : {LLM_MODEL}")
    print(f"  Mode       : {'stateful (state_check)' if args.stateful else 'stateless (dynamic_script)'}")
    print(f"  Output     : {output_path}")
    print("-" * 60)

    success_count = 0
    fail_count = 0

    with output_path.open("w", encoding="utf-8") as f:
        for idx, (bucket, hallucination_type, difficulty) in enumerate(combos, 1):
            print(f"[{idx}/{len(combos)}] {bucket} | {hallucination_type} | {difficulty}")

            template = load_prompt_template(hallucination_type, stateful=args.stateful)
            prompt = build_prompt(
                template,
                hallucination_type=hallucination_type,
                bucket=bucket,
                difficulty=difficulty,
                tools=tools_by_bucket[bucket],
            )

            task = generate_task_with_llm(prompt, client)

            if task:
                # stateful 模式：校验生成结果结构
                if args.stateful and not _validate_stateful(task):
                    fail_count += 1
                    print(f"  ✗ Failed (stateful validation: strategy={task.get('ground_truth', {}).get('strategy')!r}, "
                          f"assertions={len(task.get('ground_truth', {}).get('state_assertions', []))})")
                    continue

                task["_generation_metadata"] = {
                    "bucket": bucket,
                    "hallucination_type": hallucination_type,
                    "difficulty": difficulty,
                    "stateful": args.stateful,
                }
                f.write(json.dumps(task, ensure_ascii=False) + "\n")
                f.flush()  # 每条立即写盘，避免中途崩溃丢失
                success_count += 1
                print(f"  ✓ OK")
            else:
                fail_count += 1
                print(f"  ✗ Failed")

    print("-" * 60)
    print(f"Done.  Success: {success_count}  Failed: {fail_count}")
    print(f"Saved to: {output_path}")


if __name__ == "__main__":
    main()

