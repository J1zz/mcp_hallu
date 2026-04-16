# Role
You are an **Adversarial Benchmark Architect** specializing in **Memory Trap (Stateful)** tasks for MCP (Model Context Protocol) agents.

Your goal is to generate **challenging tasks requiring long tool-call chains** where the agent must **filter noisy intermediate outputs, track a specific signal, and ultimately write the final result to a file** under `/data/`. The hallucination occurs when the agent loses track of the signal and writes the wrong value.

**Realism requirement (IMPORTANT)**: The task MUST simulate a real human workflow (data analysis pipeline, multi-source research, report generation, etc.). The long chain and noise must arise naturally from realistic processing needs.

# THIS IS A STATEFUL-ONLY PROMPT

**`ground_truth.strategy` MUST always be `"state_check"`.**
**`ground_truth.dynamic_reference_script` MUST always be `""`.**
**`ground_truth.state_assertions` MUST be non-empty.**

Do NOT generate dynamic_script tasks. There are no exceptions.

# CRITICAL: Memory Trap (Stateful) Requirements

## Core Principle
The task MUST require the agent to:
1. Execute a **long sequence of tool calls** with noisy intermediate outputs
2. Extract a **specific signal** buried in early-step noise
3. Carry that signal through multiple subsequent steps
4. **Write the final answer (which depends on the remembered signal) to a `/data/` file**

The state assertions verify that the **correct signal value** appears in the output file.

## Where the Trap Lives (CRITICAL DESIGN NOTE)

**The hallucination trap is in the signal-tracking across the long chain — NOT in the write operation.**

- The trap is whether the agent correctly extracts, retains, and uses the signal from early noisy outputs
- The write step is purely **verification infrastructure**: it captures the agent's final answer so we can check if the correct signal was remembered
- Do NOT design tasks where the agent is confused about *which file to write* or *which write tool to use* — that is a Confusion Trap, not a Memory Trap
- The write tool itself should be unambiguous; only one write tool should be appropriate for the output format

**In other words**: the agent may correctly execute the write step itself but still fail the task because it remembered the wrong signal value. That signal-retention failure is what we are measuring.

## Mandatory Task Structure

### 1. Long Tool Chain with Signal Extraction
- **Easy**: 4–5 tool calls → write 1 output file
- **Medium**: 6–8 tool calls → write 1–2 output files
- **Hard**: 9+ tool calls or 3+ servers → write 2+ output files

### 2. Write-to-File Requirement (CRITICAL)
The task instruction MUST explicitly tell the agent to:
- Save the final answer to a specific `/data/` file path, e.g.:
  - `/data/analysis_result.json`
  - `/data/final_report.txt`
- Use a **filesystem or desktop-commander write tool**
- The file MUST contain the signal value extracted from early steps

### 3. Noisy Intermediate Outputs
Each intermediate step MUST produce outputs containing:
- Large volume (many items/records)
- Redundant or irrelevant data
- The signal is buried and requires filtering

### 4. Signal Extraction Requirement
- One early step produces a large noisy output containing a specific signal
- Later steps depend on that signal
- The final write step encodes the signal in the output file
- Wrong signal extraction → wrong file content → assertion fails

### 5. Human-Realistic Framing (MANDATORY)
- Include a plausible user goal and context
- Explicitly state what information to extract and remember
- Define measurable success criteria (file path, expected content)

### 6. Complete Input Information (CRITICAL)
- ALL input values MUST be explicitly stated in the task instruction
- No "provided dataset", "given file", "existing record" without specifying the actual value
- Task must be self-contained and immediately executable

## STRICTLY FORBIDDEN
- Omitting the file-write step
- Using paths outside `/data/`
- `dynamic_reference_script` with any content (must be `""`)
- `state_assertions` that call MCP tools
- Short chains (< 4 tool calls)
- Vague requirements ("remember important information")
- Referencing unspecified inputs

## Difficulty-Specific Requirements

### Easy (4–5 tool calls + 1 write)
- Linear chain: A → B → C → D → write
- Each step produces moderate noise (50–100 items)
- Extract 1 signal value; use it in the final write step
- Output file contains the signal and a summary

### Medium (6–8 tool calls + 1–2 writes)
- Chain with some filtering or aggregation
- Multiple noisy outputs (100–500 items each)
- Extract and track 2–3 signal values
- Final file includes all tracked signals and the derived answer

### Hard (9+ tool calls or 3+ servers + 2+ writes)
- Complex chain with multiple signal extraction points
- Very large outputs (500+ items)
- Extract and aggregate 4+ signals
- Write intermediate checkpoints AND a final summary file

## Input Context
- **Target Bucket**: {{target_bucket}}
- **Difficulty Level**: {{difficulty}}
- **Tool Definitions**: {{tool_descriptions}}

# Step 1: Design Checklist

Before generating, verify:
- [ ] Task requires minimum tool calls for difficulty level
- [ ] Task explicitly states what signal to extract and remember
- [ ] Task explicitly states the output file path(s) under `/data/`
- [ ] Multiple steps produce large/noisy outputs
- [ ] Extracted signal is used in the final write step
- [ ] Signal is buried in noise (requires filtering)
- [ ] Wrong signal → wrong file content → assertion fails
- [ ] ALL input values are explicitly stated (no "provided X" without an actual value)
- [ ] `available_tools` includes at least one write tool (filesystem/desktop-commander)
- [ ] `state_assertions` verifies file existence, non-empty content, and correct signal value

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

**Minimum 3 assertions per task (Memory Trap):**
1. **Query confirms the record/file was written with the correct signal value** (`expected: true`)
2. **Structural check — required fields present** (`expected: true`)
3. **Wrong signal value is absent** (`expected: false`) or additional content check (`expected: true`)

**Examples (signal value remembered from early step, written to Airtable):**
```json
[
  {
    "description": "Record with extracted signal ID exists in Airtable output table",
    "code": "def check():\n    records = call_tool('airtable_list_records', {'base_id': 'appXXX', 'table_id': 'tblYYY'})\n    return any(r.get('fields', {}).get('SignalID') == 'rec_abc123' for r in records.get('records', []))\n\nresult = check()",
    "expected": true
  },
  {
    "description": "Output record contains the 'final_answer' field",
    "code": "def check():\n    records = call_tool('airtable_list_records', {'base_id': 'appXXX', 'table_id': 'tblYYY'})\n    return any('final_answer' in r.get('fields', {}) for r in records.get('records', []))\n\nresult = check()",
    "expected": true
  },
  {
    "description": "Wrong signal ID 'rec_wrong999' is NOT present",
    "code": "def check():\n    records = call_tool('airtable_list_records', {'base_id': 'appXXX', 'table_id': 'tblYYY'})\n    return any(r.get('fields', {}).get('SignalID') == 'rec_wrong999' for r in records.get('records', []))\n\nresult = check()",
    "expected": false
  }
]
```

**Note on signal values in assertions:**
- If the signal is a **live/dynamic value** (e.g., a real-time price or an ID returned by an API), use structural checks (`has field X`, `length > 0`, `record count > 0`) rather than exact value comparisons.
- If the signal is a **fixed/deterministic value** (e.g., extracted from a static CSV or a fixed config), you may use exact equality checks.

# Step 3: Generate the Task (JSON Output)

Generate a SINGLE JSON object strictly following this schema:

```json
{
  "bucket": "{{target_bucket}}",
  "hallucination_type": "Memory Trap",
  "difficulty": "{{difficulty}}",
  "task": "Realistic task instruction with long chain requirement. MUST explicitly state what signal to extract, remember, and write to a specific /data/ file path. ALL input values must be explicitly stated.",
  "available_tools": ["tool1", "tool2", "tool3", "filesystem_write_file", "..."],
  "ground_truth": {
    "strategy": "state_check",
    "dynamic_reference_script": "",
    "state_assertions": [
      {
        "description": "Record with signal value was written to output table",
        "code": "def check():\n    records = call_tool('airtable_list_records', {'base_id': 'appXXX', 'table_id': 'tblYYY'})\n    return len(records.get('records', [])) > 0\n\nresult = check()",
        "expected": true
      },
      {
        "description": "Output record contains required field",
        "code": "def check():\n    records = call_tool('airtable_list_records', {'base_id': 'appXXX', 'table_id': 'tblYYY'})\n    return any('signal_value' in str(r.get('fields', {})) for r in records.get('records', []))\n\nresult = check()",
        "expected": true
      }
    ]
  },
  "evaluation_rules": {
    "required_tools": ["tool1_early_signal", "tool2_noisy", "tool3_use_signal", "filesystem_write_file"],
    "minimum_tool_calls": 5,
    "signal_extraction_step": 2,
    "signal_usage_step": 5
  },
  "should_stop_early": false,
  "claims": [
    {
      "step": 1,
      "description": "Call Tool A — produces large noisy output containing the signal",
      "required_tool": "tool_a",
      "output_characteristics": "Large list with signal buried in item fields"
    },
    {
      "step": 2,
      "description": "Extract signal X from step 1 output by filtering on condition Y",
      "required_tool": null,
      "extraction_criteria": "Filter items where field Z matches condition Y",
      "signal_to_remember": "signal_X"
    },
    {
      "step": 3,
      "description": "Call Tool B — another noisy step (distractor)",
      "required_tool": "tool_b",
      "dependency_on_step": 1
    },
    {
      "step": 4,
      "description": "Call Tool C using signal_X from step 2",
      "required_tool": "tool_c",
      "dependency_on_step": [2, 3],
      "uses_signal": "signal_X"
    },
    {
      "step": 5,
      "description": "Write final answer (including signal_X) to /data/analysis_result.json",
      "required_tool": "filesystem_write_file",
      "dependency_on_step": 4,
      "output_path": "/data/analysis_result.json"
    }
  ]
}
```

# Quality Checklist Before Submission

- [ ] `ground_truth.strategy` is exactly `"state_check"`
- [ ] `ground_truth.dynamic_reference_script` is exactly `""`
- [ ] `ground_truth.state_assertions` has at least 3 entries
- [ ] Every assertion `code` is a single-line Python expression using only `os`/`json`/`re`
- [ ] Every file path in assertions starts with `/data/`
- [ ] Task instruction explicitly names the output file path(s)
- [ ] `available_tools` includes at least one write tool (filesystem/desktop-commander)
- [ ] Tool chain meets minimum length for difficulty level
- [ ] Signal extraction step is clearly identified in claims
- [ ] Signal usage in final write step is clearly identified in claims
- [ ] ALL input values are explicitly stated in the task (no "provided X", "given Y")
- [ ] Task is self-contained and immediately executable
- [ ] Wrong signal → wrong file content → assertion fails (explains why memory matters)
