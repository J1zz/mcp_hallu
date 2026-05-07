import argparse
import json
import os
import re
import time
from pathlib import Path
from itertools import product
import requests

"""# Generate Memory Trap tasks (ANALYTICS + FINANCIAL, all difficulties)
uv run python generate_tasks_from_prompt.py \
  --type memory \
  --output tasks/memory_v2_tasks.jsonl

# Generate only a specific bucket and difficulty
uv run python generate_tasks_from_prompt.py \
  --type reasoning \
  --output tasks/reasoning_tasks.jsonl \
  --buckets ANALYTICS \
  --difficulties Hard

# Generate other types (reasoning / confusion / void)
uv run python generate_tasks_from_prompt.py \
  --type void \
  --output tasks/void_tasks.jsonl

# Generate all 4 types × all buckets
uv run python generate_tasks_from_prompt.py \
  --output tasks/all_v2_tasks.jsonl"""
# Use script directory as root (compatible with any working directory)
ROOT = Path(__file__).parent
TOOLS_FILE_STATELESS = ROOT / "tools_discription/ list-tools_bucketed_readonly.json"
TOOLS_FILE_STATEFUL  = ROOT / "tools_discription/list-tools_bucketed.json"
PROMPTS_DIR = ROOT / "prompts"

# Prompt file mapping (stateless: dynamic_script)
PROMPT_FILES = {
    "Reasoning Trap": PROMPTS_DIR / "reasoning_trap_prompt.md",
    "Void Trap":      PROMPTS_DIR / "void_trap_prompt.md",
    "Confusion Trap": PROMPTS_DIR / "confusion_trap_prompt.md",
    "Memory Trap":    PROMPTS_DIR / "memory_trap_prompt.md",
}

# Prompt file mapping (stateful: state_check)
PROMPT_FILES_STATEFUL = {
    "Reasoning Trap": PROMPTS_DIR / "reasoning_trap_stateful_prompt.md",
    "Memory Trap":    PROMPTS_DIR / "memory_trap_stateful_prompt.md",
    "Confusion Trap": PROMPTS_DIR / "confusion_trap_stateful_prompt.md",
}

# Short type name → full name, for convenient CLI arg passing
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
# LLM configuration (environment variables take priority)
LLM_MODEL = os.getenv("LLM_MODEL", "anthropic.claude-opus-4-7")
LLM_API_KEY = os.getenv("LLM_API_KEY","")
LLM_BASE_URL = os.getenv("LLM_BASE_URL","")

# Rate limiting configuration (environment variables take priority)
# RPM_DELAY: forced wait seconds after each request for proactive rate limiting (e.g. RPM=10 → set to 6)
# MAX_RETRIES: maximum retry attempts on 429/5xx errors
# RETRY_BASE_WAIT: initial wait seconds for exponential backoff (doubles each time)
RPM_DELAY = float(os.getenv("RPM_DELAY", "0"))       # 0 = no proactive rate limiting
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
    """Validate the structure of a stateful task:
    - strategy == state_check
    - state_assertions is non-empty
    - dynamic_reference_script is empty
    - each assertion's code is syntactically valid and contains a 'result =' assignment
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
    """Extract a JSON object from a model response (compatible with Gemini multi-line code block output)"""
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
        # Take the longest match (most likely the complete JSON)
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
                        break  # Found matching brackets but parsing failed, no further attempts

    return None


def generate_task_with_llm(prompt: str) -> dict | None:
    """
    Generate a task using LLM with exponential backoff retry + proactive rate limiting.
    Directly calls the OpenAI-compatible endpoint via requests.

    Retry strategy:
      - HTTP 429: server-side rate limit, wait with exponential backoff then retry
      - HTTP 5xx: transient server error, wait with exponential backoff then retry
      - Network error: brief wait then retry
      - Other 4xx: fail immediately, no retry

    Proactive rate limiting:
      - When RPM_DELAY > 0, wait RPM_DELAY seconds after each successful request
    """
    endpoint = LLM_BASE_URL.rstrip("/") + "/chat/completions"
    headers = {
        "Content-Type": "application/json",
        "Authorization": f"Bearer {LLM_API_KEY}",
    }
    payload = {
        "model": LLM_MODEL,
        "system": "You are a task generation assistant. Always respond with valid JSON only, no additional text.",
        "messages": [
            {"role": "user", "content": prompt},
        ],
        "max_tokens": 18192,
    }

    for attempt in range(1, MAX_RETRIES + 1):
        try:
            response = requests.post(
                endpoint,
                headers=headers,
                json=payload,
                timeout=(10.0, 180.0),
            )

            if response.status_code == 429:
                wait = RETRY_BASE_WAIT * (2 ** (attempt - 1))
                if attempt >= MAX_RETRIES:
                    print(f"  RateLimitError (429), max retries reached, giving up")
                    return None
                print(f"  RateLimitError (429), retrying in {wait:.0f}s [{attempt}/{MAX_RETRIES}]...")
                time.sleep(wait)
                continue

            if response.status_code >= 500:
                wait = RETRY_BASE_WAIT * (2 ** (attempt - 1))
                if attempt >= MAX_RETRIES:
                    print(f"  ServerError {response.status_code}, max retries reached, giving up")
                    return None
                print(f"  ServerError {response.status_code}, retrying in {wait:.0f}s [{attempt}/{MAX_RETRIES}]...")
                time.sleep(wait)
                continue

            if response.status_code != 200:
                print(f"  Error {response.status_code}: {response.text[:300]}")
                return None

            body = response.json()

            # Compatible with OpenAI format (choices) and Anthropic native format (content)
            if "choices" in body:
                choice = body["choices"][0]
                if choice.get("finish_reason") == "length":
                    print(f"  Warning: Response truncated (finish_reason=length), increase max_tokens")
                    return None
                content = choice["message"]["content"]
            elif "content" in body:
                if body.get("stop_reason") == "max_tokens":
                    print(f"  Warning: Response truncated (stop_reason=max_tokens), increase max_tokens")
                    return None
                text_blocks = [b.get("text", "") for b in body["content"] if b.get("type") == "text"]
                content = "\n".join(text_blocks)
            else:
                print(f"  Warning: Unexpected response format: {str(body)[:300]}")
                return None
            if not content:
                print("  Warning: Empty response from LLM")
                return None

            task = extract_json_from_response(content)
            if task is None:
                print(f"  Warning: Failed to parse JSON: {content[:200]}...")
                return None

            # Proactive rate limiting wait after successful request
            if RPM_DELAY > 0:
                time.sleep(RPM_DELAY)

            return task

        except requests.exceptions.ConnectionError as e:
            wait = RETRY_BASE_WAIT * (2 ** (attempt - 1))
            if attempt >= MAX_RETRIES:
                print(f"  ConnectionError: max retries reached, giving up")
                return None
            print(f"  ConnectionError, retrying in {wait:.0f}s [{attempt}/{MAX_RETRIES}]...")
            time.sleep(wait)

        except requests.exceptions.Timeout as e:
            wait = RETRY_BASE_WAIT * (2 ** (attempt - 1))
            if attempt >= MAX_RETRIES:
                print(f"  Timeout: max retries reached, giving up")
                return None
            print(f"  Timeout, retrying in {wait:.0f}s [{attempt}/{MAX_RETRIES}]...")
            time.sleep(wait)

        except Exception as e:
            print(f"  Error calling LLM: {e}")
            return None

    return None


def load_prompt_template(hallucination_type: str, stateful: bool = False) -> str:
    """Load the corresponding prompt template based on hallucination type and mode."""
    prompt_files = PROMPT_FILES_STATEFUL if stateful else PROMPT_FILES
    prompt_file = prompt_files.get(hallucination_type)
    if not prompt_file or not prompt_file.exists():
        raise FileNotFoundError(
            f"Prompt file not found for {hallucination_type} (stateful={stateful}): {prompt_file}"
        )
    return prompt_file.read_text(encoding="utf-8")


def main():
    parser = argparse.ArgumentParser(
        description="Generate benchmark tasks using the specified hallucination type prompt",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""
Example usage:
  # Generate Memory Trap for all bucket × difficulty combinations (stateless)
  uv run python generate_tasks_from_prompt.py --type memory --output tasks/memory_v2.jsonl

  # Generate stateful Reasoning Trap (any bucket)
  uv run python generate_tasks_from_prompt.py --type reasoning --stateful \\
      --output tasks/reasoning_stateful.jsonl

  # Generate only ANALYTICS bucket with Easy/Medium (stateless)
  uv run python generate_tasks_from_prompt.py --type memory --output tasks/memory_v2.jsonl \\
      --buckets ANALYTICS --difficulties Easy Medium

  # Generate all stateless types (omit --type to generate all)
  uv run python generate_tasks_from_prompt.py --output tasks/all_tasks.jsonl
        """,
    )
    parser.add_argument(
        "--type",
        dest="hallu_type",
        default=None,
        help="Hallucination type (memory/reasoning/confusion/void); omit to generate all types",
    )
    parser.add_argument(
        "--output",
        required=True,
        help="Output file path (relative to task_generation/ or absolute), e.g. tasks/memory_v2.jsonl",
    )
    parser.add_argument(
        "--buckets",
        nargs="+",
        default=None,
        help="Generate tasks only for the specified buckets, e.g. --buckets ANALYTICS FINANCIAL (default: all buckets)",
    )
    parser.add_argument(
        "--difficulties",
        nargs="+",
        default=["Easy", "Medium", "Hard"],
        help="Difficulty levels (default: Easy Medium Hard)",
    )
    parser.add_argument(
        "--stateful",
        action="store_true",
        help="Generate state_check type tasks (agent must write file state). Only supported for Reasoning Trap / Memory Trap.",
    )
    args = parser.parse_args()

    if not LLM_API_KEY:
        print("Error: LLM_API_KEY not set (env var or default)")
        return

    # Resolve hallucination type
    if args.hallu_type:
        resolved = TYPE_ALIASES.get(args.hallu_type)
        if not resolved:
            print(f"Error: unknown type '{args.hallu_type}'. Choose from: memory, reasoning, confusion, void")
            return
        # stateful mode only supports Reasoning/Memory/Confusion Trap
        if args.stateful and resolved not in PROMPT_FILES_STATEFUL:
            print(f"Error: --stateful does not support '{resolved}', only Reasoning Trap / Memory Trap / Confusion Trap")
            return
        hallucination_types = [resolved]
    else:
        if args.stateful:
            hallucination_types = list(PROMPT_FILES_STATEFUL.keys())
        else:
            hallucination_types = list(PROMPT_FILES.keys())

    # Verify prompt files exist
    prompt_map = PROMPT_FILES_STATEFUL if args.stateful else PROMPT_FILES
    for ht in hallucination_types:
        pf = prompt_map[ht]
        if not pf.exists():
            print(f"Error: Prompt file not found: {pf}")
            return

    # Output path
    output_path = Path(args.output)
    if not output_path.is_absolute():
        output_path = ROOT / args.output
    output_path.parent.mkdir(parents=True, exist_ok=True)

    # stateful mode uses the full tool set (including write ops), stateless uses read-only tool set
    tools_file = TOOLS_FILE_STATEFUL if args.stateful else TOOLS_FILE_STATELESS
    tools_by_bucket = load_tools_by_bucket(tools_file)
    print(f"  Tools file : {tools_file.name}  ({sum(len(v) for v in tools_by_bucket.values())} tools)")

    # Filter buckets
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

            task = generate_task_with_llm(prompt)

            if task:
                # stateful mode: validate generated task structure
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
                f.flush()  # Flush immediately after each record to avoid data loss on crash
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

