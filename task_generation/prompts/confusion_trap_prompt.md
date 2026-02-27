# Role
You are an **Adversarial Benchmark Architect** specializing in **Confusion Trap** tasks for MCP (Model Context Protocol) agents.

Your goal is to generate **challenging tasks that exploit semantic overlap between similar tools**, forcing the agent to carefully distinguish between "twin" tools and select the correct one based on subtle differences.

**Realism requirement (IMPORTANT)**: The task MUST simulate a real human workflow (searching for information, querying data, processing files, etc.). It must not feel like an artificial puzzle. The tool selection challenge must arise naturally from realistic requirements (output format needs, parameter constraints, capability requirements).

# CRITICAL: Confusion Trap Requirements

## Core Principle
The task MUST present a scenario where **multiple tools appear equally suitable** at first glance, but only **one tool is actually correct** when considering:
- Parameter structure and requirements
- Output schema and data format
- Side effects (read vs write, etc.)
- Specific capabilities and limitations

**CRITICAL**: The confusion tools (wrong tools) **MUST be executable** - they should NOT fail to run. However, their execution results **MUST fail to meet the task requirements**:
- Wrong tool executes successfully but returns data in wrong format
- Wrong tool executes successfully but lacks required information/fields
- Wrong tool executes successfully but requires incompatible parameters
- Wrong tool executes successfully but produces side effects that prevent task completion

The failure should be in **task completion**, not in **tool execution**.

## Mandatory Structure

### 1. Semantic Overlap
- **MUST** identify at least 2 tools with overlapping or similar semantics
- Tools should appear to solve the same problem
- **Confusion tools can span multiple servers** - tools from different servers can be confusing if they have similar semantics
- Example pairs (can be from same or different servers):
  - `search` vs `query` vs `find` (from search server vs query server)
  - `get_weather` vs `get_weather_forecast` (from weather server)
  - `list_files` vs `list_directory` vs `scan_directory` (from filesystem server)
  - `create_record` vs `add_entry` vs `insert_item` (from database server vs api server)

### 2. Subtle Differentiation
Only ONE tool is correct because of:
- **Parameter differences**: One requires specific format/type the other doesn't
- **Output differences**: One returns data in the needed format, the other doesn't
- **Side effect differences**: One is read-only, the other modifies state
- **Capability differences**: One supports the specific operation, the other has limitations

**IMPORTANT**: Confusion tools (wrong tools) **MUST be executable**:
- They should execute without errors
- They should return valid results
- BUT their results fail to meet task requirements (wrong format, missing fields, incompatible structure, etc.)
- The agent should be able to call the wrong tool and get a result, but realize it doesn't satisfy the task needs

### 3. Indirect Correctness Signal
- The task instruction **MUST** imply the correct tool indirectly
- It should NOT explicitly name the tool
- The correct choice should be inferable from:
  - Required output format mentioned in task
  - Specific operation type needed
  - Constraints or requirements that match one tool better

### 4. Human-Realistic Problem Framing (MANDATORY)
- The task MUST include a plausible user goal and context (who/when/why), e.g.:
  - searching for information with specific format requirements
  - querying data that needs to be processed in a particular way
  - retrieving information for a specific use case
- The task MUST define measurable success criteria:
  - what format the output should be in
  - what specific information is needed
  - what constraints must be met

### 5. Complete Input Information (CRITICAL)
- The task MUST contain **ALL necessary input information** explicitly stated in the instruction
- The task MUST NOT assume any information exists outside the instruction (e.g., "provided query", "given keyword", "the data you have")
- **ALL input values MUST be explicitly specified** in the task text:
  - If the task requires a search query, it MUST say: "搜索 'restaurants in Tokyo'" (not "根据提供的查询搜索")
  - If the task requires a keyword, it MUST say: "查询关键词 'weather'" (not "使用给定的关键词查询")
  - If the task requires a location, it MUST say: "查询纽约的天气" (not "查询提供的位置")
- The task must be **self-contained and executable** without requiring additional context or external inputs
- Example of CORRECT task: "搜索 'best restaurants in Tokyo' 并返回 JSON 格式的结果，包含评分和价格信息。"
- Example of WRONG task: "根据提供的查询搜索并返回结果。" (missing: what is the query? what format is needed?)

## STRICTLY FORBIDDEN
- Tasks where multiple tools are equally valid (both would work)
- Tasks with ambiguous wording that makes tool choice arbitrary
- Tasks that explicitly name the tool to use
- Tasks where the difference is too obvious (not a real trap)
- **Tasks that reference unspecified inputs**: "根据提供的...", "使用给定的...", "the provided...", "the given..." (without actually providing the value)
- **Tasks that assume external context**: "查询你有的数据", "处理现有的文件", "根据之前的结果" (without specifying what the data/file/result is)
- **Incomplete task descriptions**: Any task that requires information not explicitly stated in the instruction itself

## Difficulty-Specific Requirements

### Easy (2-3 tools)
- Two similar tools, one clearly correct based on parameter/output
- Example: "Search for restaurants" - one tool returns structured data with ratings, the other returns plain text. Task needs ratings, so first tool is correct.

### Medium (4-5 tools)
- 3+ similar tools, need to check multiple attributes to find correct one
- OR nested requirement: correct tool depends on another tool's output
- Example: "Get weather data and format as JSON" - multiple weather tools, but only one returns JSON natively vs requiring conversion

### Hard (6+ tools or 3+ servers)
- Multiple overlapping tool pairs across **different servers** (confusion tools can span servers)
- OR complex requirement chain where tool choice depends on multiple factors
- Example: "Search for locations, get details, and save to database" - multiple search tools from different servers (one returns structured JSON, another returns plain text), multiple detail tools (one returns full schema, another returns summary), multiple save tools (one supports the data format from detail tool, another doesn't). Must choose compatible chain. Wrong tools execute but produce incompatible results.

## Input Context
- **Target Bucket**: {{target_bucket}}
- **Difficulty Level**: {{difficulty}}
- **Tool Definitions**: {{tool_descriptions}}

# Step 1: Tool Analysis for Confusion Trap

**CRITICAL**: You MUST strictly use the provided `Tool Definitions`. Do NOT invent tools.

1. **Identify Twin Tools**:
   - Find tools with similar names/semantics
   - **Can span multiple servers** - tools from different servers can be confusing
   - Group them by apparent functionality
   - Example: `search_X` from server A, `query_X` from server B, `find_X` from server C all seem to do similar things

2. **Analyze Differences**:
   - Compare parameter structures (required vs optional, types, formats)
   - Compare output schemas (what data is returned, format)
   - Compare side effects (read-only vs write, etc.)
   - Compare capabilities (limits, supported operations)
   - **Verify confusion tools are executable** - they should run successfully but produce results that don't meet task requirements

3. **Design Task with Indirect Signal**:
   - Create task that requires specific capability/format
   - Make it match ONE tool's strengths
   - Ensure other "twin" tools execute successfully but produce results that fail to meet task requirements (wrong format, missing fields, etc.)

# Step 2: Task Design Checklist

Before generating, verify:
- [ ] At least 2 tools with semantic overlap identified
- [ ] Only ONE tool is actually correct for the task
- [ ] Task instruction implies correct tool indirectly (not explicitly)
- [ ] **ALL input values are explicitly stated in the task (no "provided", "given", "existing" without actual values)**
- [ ] **Task is self-contained and executable without external context**
- [ ] Wrong tool selection would execute successfully but fail to meet task requirements
- [ ] Confusion tools are executable (not broken/non-functional)
- [ ] Confusion tools can span multiple servers
- [ ] Difference between tools is subtle but discoverable
- [ ] Task doesn't make multiple tools equally valid
- [ ] Difficulty level matches number of similar tools and complexity
- [ ] `available_tools` includes ALL tools from servers used in the task (for confusion)
- [ ] `dynamic_reference_script` is a complete, executable Python function that returns a string containing all execution records and results

# Step 3: Generate the Task (JSON Output)

## Ground Truth 策略选择规则（必须根据使用的tools选择，不能写死）
你必须根据 **tools类型** 选择 `ground_truth.strategy`，并填充对应字段：

- **A. 无状态/查询类任务**
  - **strategy**: `dynamic_script`
  - **dynamic_reference_script**: 必填，必须是一个**可直接执行的 Python 函数**（函数名建议为 `generate_reference_answer`）
    - 函数内部使用 `from mcp_client import call_tool` 调用真实 MCP 工具
    - 函数必须**返回一个字符串**，包含所有执行记录和结果（用于后续传给 LLM 生成 reference answer）
    - 返回的字符串必须包含：工具选择过程、为什么选择正确工具、为什么其他工具不正确、执行结果
    - 函数格式示例：
      ```python
      def generate_reference_answer():
          import json
          from mcp_client import call_tool
          
          execution_log = []
          
          # 分析工具选择
          execution_log.append("Step 1: Analyzing task requirements...")
          execution_log.append("Task requires: [specific format/parameter/capability]")
          execution_log.append("Available similar tools: tool_a (server_a), tool_b (server_b), tool_c (server_c)")
          
          # 尝试混淆工具（展示它们可以执行但结果不对）
          execution_log.append("Step 2: Testing confusion tools...")
          execution_log.append("Attempting tool_b (server_b) - executes successfully:")
          try:
              wrong_result = call_tool('server_b', 'tool_b', {...})
              wrong_data = json.loads(wrong_result) if isinstance(wrong_result, str) else wrong_result
              execution_log.append(f"Tool_b result: {json.dumps(wrong_data, indent=2)}")
              execution_log.append("Problem: [e.g., returns plain text instead of JSON, missing required fields, etc.]")
          except Exception as e:
              execution_log.append(f"ERROR: Tool should be executable but failed: {e}")
          
          # 选择正确工具并执行
          execution_log.append("Step 3: Selected tool_a (server_a) because [reason]")
          execution_log.append("Calling tool 'server_a.tool_a' with params {...}")
          result = call_tool('server_a', 'tool_a', {...})
          result_data = json.loads(result) if isinstance(result, str) else result
          execution_log.append(f"Result: {json.dumps(result_data, indent=2)}")
          execution_log.append("This result meets task requirements: [explain why]")
          
          # 说明为什么其他工具不正确（但它们可以执行）
          execution_log.append("Step 4: Why tool_b is wrong: [reason - but it executes successfully]")
          execution_log.append("Why tool_c is wrong: [reason - but it executes successfully]")
          
          return "\\n".join(execution_log)
      ```
  - **state_assertions**: 必须是空数组 `[]`

- **B. 有状态/操作类任务**
  - **strategy**: `state_check`
  - **dynamic_reference_script**: 必须为空字符串 `\"\"`（或保持为空）
  - **state_assertions**: 必填（用于断言 Agent 执行后的世界/文件/仓库状态）

Generate a SINGLE JSON object strictly following this schema:

```json
{
  "bucket": "{{target_bucket}}",
  "hallucination_type": "Confusion Trap",
  "difficulty": "{{difficulty}}",
  "task": "Task instruction that indirectly signals the correct tool from semantically similar options. CRITICAL: ALL input values (queries, keywords, locations, etc.) MUST be explicitly stated in the task text. Do NOT use phrases like 'provided query' or 'given keyword' without actually providing the value.",
  "available_tools": ["tool1", "tool2", "tool3", "tool4", ...],
  // CRITICAL: available_tools MUST include ALL tools from ALL servers used in this task
  // This is for confusion/distraction - the agent must select the correct tools from this list
  // Example: If task uses 'search' and 'query' servers, include ALL tools from both servers
  // Only the tools actually needed for the task should be listed in evaluation_rules.required_tools
  "ground_truth": {
    "strategy": "dynamic_script",
    "dynamic_reference_script": "def generate_reference_answer():\n    import json\n    from mcp_client import call_tool\n    \n    execution_log = []\n    \n    # 分析工具选择\n    execution_log.append(\"Step 1: Analyzing task requirements...\")\n    execution_log.append(\"Task requires: [specific format/parameter/capability]\")\n    execution_log.append(\"Available similar tools: tool_a (server_a), tool_b (server_b), tool_c (server_c)\")\n    \n    # 尝试混淆工具（展示它们可以执行但结果不对）\n    execution_log.append(\"Step 2: Testing confusion tools...\")\n    execution_log.append(\"Attempting tool_b (server_b) - executes successfully:\")\n    try:\n        wrong_result = call_tool('server_b', 'tool_b', {...})\n        wrong_data = json.loads(wrong_result) if isinstance(wrong_result, str) else wrong_result\n        execution_log.append(f\"Tool_b result: {json.dumps(wrong_data, indent=2)}\")\n        execution_log.append(\"Problem: [e.g., returns plain text instead of JSON, missing required fields, etc.]\")\n    except Exception as e:\n        execution_log.append(f\"ERROR: Tool should be executable but failed: {e}\")\n    \n    # 选择正确工具并执行\n    execution_log.append(\"Step 3: Selected tool_a (server_a) because [reason]\")\n    execution_log.append(\"Calling tool 'server_a.tool_a' with params {...}\")\n    result = call_tool('server_a', 'tool_a', {...})\n    result_data = json.loads(result) if isinstance(result, str) else result\n    execution_log.append(f\"Result: {json.dumps(result_data, indent=2)}\")\n    execution_log.append(\"This result meets task requirements: [explain why]\")\n    \n    # 说明为什么其他工具不正确（但它们可以执行）\n    execution_log.append(\"Step 4: Why tool_b is wrong: [reason - but it executes successfully]\")\n    execution_log.append(\"Why tool_c is wrong: [reason - but it executes successfully]\")\n    \n    return \"\\n\".join(execution_log)",
    "state_assertions": []
  },
  "evaluation_rules": {
    "required_tools": ["correct_tool_name"],
  },
  "should_stop_early": false,
  "claims": [
    {
      "claim": 1,
      "description": "Agent must identify tool_a, tool_b",
    },
    {
      "claim": 2,
      "description": "Agent must select tool_x but not select tool_x because xxx",
    }
  ],
  "_design_rationale": "Explain: (1) Which tools are semantically similar, (2) What makes them confusing, (3) Why only one is correct, (4) How task requirements signal the correct choice, (5) Why wrong tool selection fails, (6) Why this matches the difficulty level."
}
```

# Quality Checklist Before Submission

- [ ] At least 2 semantically similar tools identified
- [ ] Only ONE tool is correct for the task
- [ ] Task instruction implies correct tool indirectly
- [ ] **ALL input values are explicitly stated (e.g., 'restaurants in Tokyo', 'weather', not 'provided query', 'given keyword')**
- [ ] **Task is self-contained (no references to unspecified external inputs)**
- [ ] Wrong tool selection executes successfully but fails to meet task requirements
- [ ] Confusion tools are executable (not broken/non-functional)
- [ ] Confusion tools can span multiple servers
- [ ] Differentiation criteria clearly explained
- [ ] Confusion tools listed in evaluation rules with their servers
- [ ] Ground truth script is a complete, executable Python function
- [ ] Function returns a string containing all execution records and tool call results (for LLM reference answer generation)
- [ ] `available_tools` includes ALL tools from all servers used (not just required tools)
- [ ] No ambiguity that makes multiple tools equally valid

