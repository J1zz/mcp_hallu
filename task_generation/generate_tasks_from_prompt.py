import json
import os
import re
from pathlib import Path
from itertools import product
from openai import OpenAI

ROOT = Path("/Users/apple/Desktop/mcp/mcp-atlas/task_generation")
TOOLS_FILE = ROOT / "list-tools_bucketed.json"
PROMPTS_DIR = ROOT / "prompts"
OUTPUT_FILE = ROOT / "tasks/reasoning_generated_tasks.jsonl"

# Prompt 文件映射
PROMPT_FILES = {
    "Reasoning Trap": PROMPTS_DIR / "reasoning_trap_prompt.md",
    "Void Trap": PROMPTS_DIR / "void_trap_prompt.md",
    "Confusion Trap": PROMPTS_DIR / "confusion_trap_prompt.md",
    "Memory Trap": PROMPTS_DIR / "memory_trap_prompt.md",
}

# 模板占位符：{{hallucination_type}}, {{target_bucket}}, {{difficulty}}, {{tool_descriptions}}

# 默认组合，可按需修改
HALLUCINATION_TYPES = [
    "Reasoning Trap",
    # "Void Trap",
    # "Confusion Trap",
    # "Memory Trap",
]
DIFFICULTIES = ["Easy", "Medium", "Hard"]

# LLM 配置
LLM_MODEL = os.getenv("LLM_MODEL", "gpt-4o")
LLM_API_KEY = os.getenv("LLM_API_KEY", "sk-1epeJxOEZzxgTtvZ1XkFIaIJkOitgA3Sz6QUO4nvo9hvUfMN")
LLM_BASE_URL = os.getenv("LLM_BASE_URL", "http://123.129.219.111:3000/v1")


def load_tools_by_bucket():
    tools = json.loads(TOOLS_FILE.read_text())
    by_bucket = {}
    for tool in tools:
        bucket = tool.get("bucket", "UNMAPPED")
        by_bucket.setdefault(bucket, []).append(tool)
    return by_bucket


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
    """从模型响应中提取 JSON 对象"""
    if not text or not text.strip():
        return None
    
    text = text.strip()
    
    # 尝试直接解析
    try:
        return json.loads(text)
    except json.JSONDecodeError:
        pass
    
    # 尝试提取代码块中的 JSON（支持多行）
    json_pattern = r"```(?:json)?\s*(\{.*?\})\s*```"
    matches = re.findall(json_pattern, text, re.DOTALL)
    if matches:
        for match in matches:
            try:
                return json.loads(match)
            except json.JSONDecodeError:
                continue
    
    # 尝试找到第一个 { 到最后一个 } 之间的内容
    start = text.find("{")
    end = text.rfind("}")
    if start != -1 and end != -1 and end > start:
        try:
            return json.loads(text[start : end + 1])
        except json.JSONDecodeError:
            pass
    
    return None


def generate_task_with_llm(prompt: str, client: OpenAI) -> dict | None:
    """使用 LLM 生成 task"""
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
            response_format={"type": "json_object"},
        )
        
        content = response.choices[0].message.content
        if not content:
            print("Warning: Empty response from LLM")
            return None
        
        task = extract_json_from_response(content)
        if task is None:
            print(f"Warning: Failed to parse JSON from response: {content[:200]}...")
            return None
        
        return task
    except Exception as e:
        print(f"Error calling LLM: {e}")
        return None


def load_prompt_template(hallucination_type: str) -> str:
    """根据幻觉类型加载对应的 prompt 模板"""
    prompt_file = PROMPT_FILES.get(hallucination_type)
    if not prompt_file or not prompt_file.exists():
        raise FileNotFoundError(
            f"Prompt file not found for {hallucination_type}: {prompt_file}"
        )
    return prompt_file.read_text(encoding="utf-8")


def main():
    if not LLM_API_KEY:
        print("Error: LLM_API_KEY environment variable not set")
        print("Please set it with: export LLM_API_KEY=your-api-key")
        return
    
    # 验证所有 prompt 文件存在
    for hallucination_type, prompt_file in PROMPT_FILES.items():
        if not prompt_file.exists():
            print(f"Error: Prompt file not found: {prompt_file}")
            return
    
    # 初始化 OpenAI 客户端
    client_kwargs = {"api_key": LLM_API_KEY}
    if LLM_BASE_URL:
        client_kwargs["base_url"] = LLM_BASE_URL
    client = OpenAI(**client_kwargs)
    
    tools_by_bucket = load_tools_by_bucket()
    
    total_combinations = sum(
        1
        for _ in product(tools_by_bucket.keys(), HALLUCINATION_TYPES, DIFFICULTIES)
    )
    print(f"Total combinations to generate: {total_combinations}")
    print(f"Using model: {LLM_MODEL}")
    print("-" * 50)
    
    success_count = 0
    fail_count = 0
    
    with OUTPUT_FILE.open("w", encoding="utf-8") as f:
        for idx, (bucket, hallucination_type, difficulty) in enumerate(
            product(tools_by_bucket.keys(), HALLUCINATION_TYPES, DIFFICULTIES), 1
        ):
            print(
                f"[{idx}/{total_combinations}] Generating task: {bucket} | {hallucination_type} | {difficulty}"
            )
            
            # 加载对应幻觉类型的 prompt 模板
            template = load_prompt_template(hallucination_type)
            
            # 构建 prompt
            prompt = build_prompt(
                template,
                hallucination_type=hallucination_type,
                bucket=bucket,
                difficulty=difficulty,
                tools=tools_by_bucket[bucket],
            )
            
            # 调用 LLM 生成 task
            task = generate_task_with_llm(prompt, client)
            
            if task:
                # 添加元数据
                task["_generation_metadata"] = {
                    "bucket": bucket,
                    "hallucination_type": hallucination_type,
                    "difficulty": difficulty,
                }
                f.write(json.dumps(task, ensure_ascii=False) + "\n")
                success_count += 1
                print(f"  ✓ Success")
            else:
                fail_count += 1
                print(f"  ✗ Failed")
    
    print("-" * 50)
    print(f"Generation complete!")
    print(f"Success: {success_count}, Failed: {fail_count}")
    print(f"Results saved to {OUTPUT_FILE}")


if __name__ == "__main__":
    main()

