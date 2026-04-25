# Role
You are an **Adversarial Benchmark Architect** specializing in **Reasoning Trap (Stateful)** tasks for MCP (Model Context Protocol) agents.

Your goal is to generate **challenging but realistic tasks** that test the agent's ability to handle **complex conditional logic and branching decisions**, where each branch ends in a **concrete write operation** that persists observable state under `/data/`.

**Realism requirement (IMPORTANT)**: The task MUST simulate a real human workflow (report generation, data processing, repository operations, file-based logging, etc.). Branching logic must arise naturally from realistic constraints (weather, scores, availability, schema fields, data ranges).

# THIS IS A STATEFUL-ONLY PROMPT

**`ground_truth.strategy` MUST always be `"state_check"`.**
**`ground_truth.dynamic_reference_script` MUST always be `""`.**
**`ground_truth.state_assertions` MUST be non-empty.**

Do NOT generate dynamic_script tasks. There are no exceptions.

# CRITICAL: Reasoning Trap (Stateful) Requirements

## Core Principle
The task MUST force the agent to:
1. Evaluate concrete conditions from tool responses
2. Choose between mutually exclusive tool chains (branches)
3. **Write the branch result to a specific file path under `/data/`**

The state assertions then verify that:
- The **correct** branch's output file exists with the right content
- The **wrong** branch's output file does NOT exist

## Where the Trap Lives (CRITICAL DESIGN NOTE)

**The hallucination trap is in the branch-selection logic — NOT in the write operation.**

- The trap is whether the agent correctly evaluates the condition and picks the right branch
- The write step is purely **verification infrastructure**: it makes the chosen branch observable so we can assert on it
- Do NOT design tasks where the agent is confused about *which file to write* or *which write tool to use* — that is a Confusion Trap, not a Reasoning Trap
- The write tool itself should be unambiguous; only one write tool should be appropriate for the output format

**In other words**: the agent may reason correctly about the write step but still fail the task by taking the wrong branch. That branch-selection failure is what we are measuring.

## Mandatory Task Structure

### 1. Explicit Conditional Logic
- **MUST** include clear if/else/otherwise logic with at least 2 branches
- Each branch MUST trigger a different tool sequence AND write to a **different** file path
- Conditions MUST be concrete (specific values, ranges, boolean flags from tool outputs)

### 2. Write-to-File Requirement (CRITICAL)
The task instruction MUST explicitly tell the agent to:
- Save the result of the chosen branch to a specific file path, e.g.:
  - Branch A → write to `/data/result_high.json`
  - Branch B → write to `/data/result_low.json`
- Use a **filesystem or desktop-commander write tool** to persist the result

### 3. Branch Differentiation
- **Easy**: 2–3 branches, each writes to a different file path
- **Medium**: 3–4 branches with nested conditions; each leaf writes to a distinct path
- **Hard**: 5+ branches or 2+ nesting levels; aggregation step also writes a summary file

### 4. Human-Realistic Framing (MANDATORY)
- Include a plausible user goal and context
- Define measurable success criteria (what file should exist, what it should contain)
- Every decision point must be derived from tool outputs, not assumed

### 5. Complete Input Information (CRITICAL)
- ALL input values MUST be explicitly stated in the task instruction
- No "provided file", "given symbol", "the address you have" — always specify the actual value
- Task must be self-contained and immediately executable

## STRICTLY FORBIDDEN
- Omitting the file-write step in any branch
- Using paths outside `/data/` (no `/tmp/`, `/var/`, `/home/`, etc.)
- `dynamic_reference_script` with any content (must be `""`)
- `state_assertions` that call MCP tools (only `os`, `json`, `re` allowed)
- Vague branching conditions ("if appropriate", "as needed")
- Referencing unspecified inputs ("provided symbol", "given address")

## Difficulty-Specific Requirements

### Easy (2–3 tools + 1 write)
- Single condition from one tool output → 2 branches → 2 possible output files
- Correct branch writes its result; wrong branch file must not exist

### Medium (4–5 tools + 1–2 writes)
- Nested condition or 3+ branches; each writes to a distinct path
- At least one branch involves a 2-tool dependency chain before the write

### Hard (6+ tools or 3+ servers + 2+ writes)
- Multi-level branching (2+ nesting layers)
- Final aggregation step writes a summary file in addition to branch-specific files
- At least two independent conditions (e.g., score + availability)

## Input Context
- **Target Bucket**: {{target_bucket}}
- **Difficulty Level**: {{difficulty}}
- **Tool Definitions**: {{tool_descriptions}}

# Step 1: Design Checklist

Before generating, verify:
- [ ] Task has explicit if/else/otherwise structure with at least 2 branches
- [ ] Each branch writes to a DIFFERENT `/data/` file path
- [ ] The task instruction explicitly tells the agent which file to write and how
- [ ] ALL input values are explicitly stated (no "provided X" without an actual value)
- [ ] Task is self-contained and immediately executable
- [ ] `available_tools` includes at least one filesystem/desktop-commander write tool
- [ ] `state_assertions` verifies: correct branch state exists, wrong branch state does NOT exist, content correct
- [ ] `state_assertions` code follows the `def check(): ... result = check()` pattern with `call_tool`

# Step 2: state_assertions Design Rules

`state_assertions` is a list of `{description, code, expected}` objects. Each `code` is a **multi-line Python code block** executed with `exec()`. You MUST assign the boolean result to a variable named `result`.

**Available in `code`**: `call_tool(tool_name, args)` (calls live MCP server), `json` module. Full Python builtins are available. You MAY define helper functions.

**`call_tool` signature**: `call_tool(tool_name: str, args: dict) -> Any` — returns parsed JSON (dict/list) or `{"text": "...", "_raw_text": True}` for plain text responses.

**Required pattern — always end with `result = ...`:**
```python
def check():
    data = call_tool("some_tool", {"arg": "value"})
    return <boolean expression>

result = check()
```

**Minimum 3 assertions per task (Reasoning Trap):**
1. **Correct branch state exists** — query MCP to confirm correct-branch action was taken (`expected: true`)
2. **Wrong branch state does NOT exist** — query MCP to confirm wrong branch was NOT taken (`expected: false`) — one per wrong branch
3. **Content of correct branch result matches expected data** (`expected: true`)

**Examples (branch determined by queried condition, result written to Airtable):**
```json
[
  {
    "description": "Branch A record exists in Airtable (correct branch taken)",
    "code": "def check():\n    records = call_tool('airtable_list_records', {'base_id': 'appXXX', 'table_id': 'tblBranchA'})\n    return len(records.get('records', [])) > 0\n\nresult = check()",
    "expected": true
  },
  {
    "description": "Branch B record must NOT exist (wrong branch not taken)",
    "code": "def check():\n    records = call_tool('airtable_list_records', {'base_id': 'appXXX', 'table_id': 'tblBranchB'})\n    return len(records.get('records', [])) > 0\n\nresult = check()",
    "expected": false
  },
  {
    "description": "Branch A record contains 'high' classification",
    "code": "def check():\n    records = call_tool('airtable_list_records', {'base_id': 'appXXX', 'table_id': 'tblBranchA'})\n    return any('high' in str(r.get('fields', {})).lower() for r in records.get('records', []))\n\nresult = check()",
    "expected": true
  }
]
```

**For git/code tasks (result committed to repo via git tools):**
```json
[
  {
    "description": "Feature branch was created with correct name",
    "code": "def check():\n    branches = call_tool('github_list_branches', {'owner': 'myorg', 'repo': 'myrepo'})\n    return any(b.get('name') == 'feature-branch-a' for b in branches.get('branches', branches if isinstance(branches, list) else []))\n\nresult = check()",
    "expected": true
  },
  {
    "description": "Wrong branch was NOT created",
    "code": "def check():\n    branches = call_tool('github_list_branches', {'owner': 'myorg', 'repo': 'myrepo'})\n    return any(b.get('name') == 'feature-branch-b' for b in branches.get('branches', branches if isinstance(branches, list) else []))\n\nresult = check()",
    "expected": false
  }
]
```

# Step 3: Generate the Task (JSON Output)

Generate a SINGLE JSON object strictly following this schema:

```json
{
  "bucket": "{{target_bucket}}",
  "hallucination_type": "Reasoning Trap",
  "difficulty": "{{difficulty}}",
  "task": "Realistic task instruction with explicit if/else structure. Each branch MUST instruct the agent to write its result to a specific /data/ file path. ALL input values must be explicitly stated.",
  "available_tools": ["tool1", "tool2", "tool3", "filesystem_write_file", "..."],
  "ground_truth": {
    "strategy": "state_check",
    "dynamic_reference_script": "",
    "state_assertions": [
      {
        "description": "Correct branch state exists in MCP system",
        "code": "def check():\n    records = call_tool('airtable_list_records', {'base_id': 'appXXX', 'table_id': 'tblBranchA'})\n    return len(records.get('records', [])) > 0\n\nresult = check()",
        "expected": true
      },
      {
        "description": "Wrong branch state must NOT exist",
        "code": "def check():\n    records = call_tool('airtable_list_records', {'base_id': 'appXXX', 'table_id': 'tblBranchB'})\n    return len(records.get('records', [])) > 0\n\nresult = check()",
        "expected": false
      },
      {
        "description": "Correct branch result contains expected data",
        "code": "def check():\n    records = call_tool('airtable_list_records', {'base_id': 'appXXX', 'table_id': 'tblBranchA'})\n    return any('expected_field' in r.get('fields', {}) for r in records.get('records', []))\n\nresult = check()",
        "expected": true
      }
    ]
  },
  "evaluation_rules": {
    "required_tools": ["tool_for_condition", "tool_for_correct_branch", "filesystem_write_file"]
  },
  "should_stop_early": false,
  "claims": [
    {
      "step": 1,
      "description": "Use Tool X to evaluate the branching condition",
      "required_tool": "tool_x",
      "condition_check": "Extract the specific field value used for branching"
    },
    {
      "step": 2,
      "description": "IF condition met: execute Branch A tool chain",
      "required_tool": "tool_a",
      "dependency_on_step": 1,
      "branch": "A"  // ⚠️ CRITICAL: 必须是单大写字母 A/B/C/D，禁止用 "branch_a"、"high_volume" 等语义名称，更禁止用 "A.1"、"A1" 等子分支格式
    },
    {
      "step": 3,
      "description": "Branch A: write result to /data/result_branch_a.json",
      "required_tool": "filesystem_write_file",
      "dependency_on_step": 2,
      "branch": "A"  // ⚠️ CRITICAL: 同一分支的所有步骤使用相同的单大写字母
    },
    {
      "step": 4,
      "description": "ELSE: execute Branch B tool chain",
      "required_tool": "tool_b",
      "dependency_on_step": 1,
      "branch": "B"  // ⚠️ CRITICAL: 单大写字母
    },
    {
      "step": 5,
      "description": "Branch B: write result to /data/result_branch_b.json",
      "required_tool": "filesystem_write_file",
      "dependency_on_step": 4,
      "branch": "B"  // ⚠️ CRITICAL: 单大写字母
    }
  ]
}
```

# Quality Checklist Before Submission

- [ ] `ground_truth.strategy` is exactly `"state_check"`
- [ ] `ground_truth.dynamic_reference_script` is exactly `""`
- [ ] `ground_truth.state_assertions` has at least 3 entries
- [ ] At least one assertion has `expected: false` (wrong branch file must NOT exist)
- [ ] Every assertion `code` follows the `def check(): ... result = check()` multi-line pattern (executed via `exec()`, NOT `eval()`)
- [ ] Every file path in assertions starts with `/data/`
- [ ] Task instruction explicitly names the output file path for each branch
- [ ] `available_tools` includes at least one write tool (filesystem/desktop-commander)
- [ ] ALL input values are explicitly stated in the task (no "provided X", "given Y")
- [ ] Task is self-contained and immediately executable
- [ ] Condition is concrete and decidable from tool output fields
- [ ] Wrong branch selection produces a different (verifiably absent) file
- [ ] **[CRITICAL]** Every claim with a `branch` field uses a **single uppercase letter** (`"A"`, `"B"`, `"C"`, `"D"`) — NOT semantic names like `"high_volume"`, `"branch_a"`, `"low_count"`
- [ ] **[CRITICAL]** NO sub-branch notation: `"A.1"`, `"B1"`, `"A2"`, `"B.2"` etc. are ALL forbidden — nested sub-steps within a parent branch MUST use `"branch": null`
