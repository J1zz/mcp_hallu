# MCPHallu: Benchmarking Reasoning, Execution, and Memory Hallucinations in MCP Agents

[![NeurIPS 2026 E&D Track](https://img.shields.io/badge/NeurIPS%202026-E%26D%20Track-blue)](https://neurips.cc)
[![License](https://img.shields.io/badge/License-MIT-green)](LICENSE)
[![Anonymous Repo](https://img.shields.io/badge/anonymous.4open.science-MCPHallu-orange)](https://anonymous.4open.science/status/mcp_hallu)

> **Double-blind submission.** Author and institution information is withheld in compliance with NeurIPS 2026 review policy. Do not de-anonymize.

---

## Overview

MCPHallu is a benchmark for diagnosing hallucinations in LLM agents operating under the [Model Context Protocol (MCP)](https://modelcontextprotocol.io/). Unlike prior benchmarks that report aggregate task-success rates, MCPHallu is designed around **structural triggers** that make one of four hallucination subtypes the dominant failure risk for each task.

### Four Hallucination Subtypes

| Subtype | Internal Label | Description |
|---|---|---|
| **Branch Collapse** | Reasoning Trap | Agent incorrectly evaluates a condition and selects the wrong execution branch |
| **Unreachable Goal** | Void Trap | Agent fails to recognize that the task is fundamentally infeasible with available tools |
| **Tool Misuse** | Confusion Trap | Agent selects a semantically similar but functionally incorrect tool |
| **Context Forgetting** | Memory Trap | Agent drops an intermediate value that a later step depends on |

### Key Statistics

- **358 tasks** across 5 capability domains (ANALYTICS, BASIC, CODING, FINANCIAL, PRODUCTIVITY)
- **3 difficulty levels**: Easy, Medium, Hard
- **36 production MCP servers** running in isolated Docker containers, covering 302 tools
- **14 frontier LLM agents** from 8 vendors evaluated on **5,014 trajectories**
- Average score: **0.642**; Unreachable Goal (Void Trap) average: **0.458**

---

## Repository Structure

```
mcp_hallu/
├── .gitignore
├── LICENSE
├── README.md
├── task_generation/               # Benchmark tasks, evaluation pipeline, and results
│   ├── final_tasks/               # ★ Canonical task files (4 subtypes + dataset metadata)
│   │   ├── confusion_tasks.jsonl
│   │   ├── memory_tasks.jsonl
│   │   ├── reasoning_tasks.jsonl
│   │   ├── void_tasks.jsonl
│   │   ├── mcphallu_tasks.json    # All tasks merged into a single JSON array
│   │   └── mcphallu_croissant.json  # Croissant metadata (ML dataset standard)
│   ├── eval/                      # Scoring modules
│   │   ├── scoring.py             # All four scoring strategies + LLM-judge wrappers
│   │   ├── runner.py              # Async evaluation runner
│   │   ├── schema.py              # Task / result data model
│   │   ├── trajectory.py          # Trajectory parsing utilities
│   │   ├── data_io.py             # JSONL / CSV I/O helpers
│   │   └── config.py              # Eval configuration (model, API keys, concurrency)
│   ├── results/                   # Per-model evaluation outputs
│   │   ├── <model>/               # One directory per evaluated model
│   │   │   ├── task_results/      # Per-task JSON score files
│   │   │   └── *.csv              # Per-subtype summary CSVs
│   │   ├── scores_summary.json    # Aggregated scores across all models
│   │   ├── scores_summary.csv     # Same data in CSV format
│   │   └── compute_scores.py      # Script to recompute summaries from task_results/
│   ├── prompts/                   # LLM prompts used for task generation (all 4 subtypes)
│   ├── tasks/                     # Seed data for task generation (Airtable CSV exports)
│   ├── tools_discription/         # Annotated tool JSON catalogues used during task design
│   ├── hallu_eval.py              # CLI: full evaluation pipeline (agent run + scoring)
│   ├── run_gt_execution.py        # CLI: pre-run GT reference scripts for dynamic tasks
│   ├── mcp_utils.py               # Shared MCP tool-call utilities
│   ├── generate_tasks_from_prompt.py  # Task generation script (LLM-assisted)
│   ├── .gitignore                 # Ignore rules for this subtree
│   ├── pyproject.toml             # Python package / dependency manifest
│   └── uv.lock                    # Locked dependency versions
└── mcp-atlas/                     # MCP server infrastructure (based on MCP-Atlas)
    ├── LICENSE
    ├── Makefile
    ├── env.template               # Environment variable template (.env)
    ├── assets/                    # Architecture diagram and paper PDF
    ├── data_exports/              # Pre-seeded database snapshots for stateful tasks
    └── services/
        ├── agent-environment/     # Dockerized MCP server environment (port 19841)
        └── mcp_eval/              # Completion service + agentic loop (port 3001)
```

---

## Quick Start

### Prerequisites

- [Docker Desktop](https://www.docker.com/products/docker-desktop/) (≥ 8 GB memory allocated)
- [uv](https://docs.astral.sh/uv/) — Python package manager
- Python 3.10–3.13
- `jq` (for CLI curl examples)

### 1. Configure environment variables

```bash
cd mcp-atlas
cp env.template .env
```

Edit `.env` and set at minimum:

| Variable | Purpose |
|---|---|
| `LLM_API_KEY` | API key for the model under evaluation |
| `EVAL_LLM_API_KEY` | API key for the LLM judge (default: `gemini/gemini-2.5-pro`) |
| `LLM_BASE_URL` | *(Optional)* Custom endpoint (LiteLLM proxy, Azure, etc.) |
| `EVAL_LLM_MODEL` | *(Optional)* Override judge model |

### 2. Start MCP servers (Docker)

```bash
# Option A — pre-built image (recommended)
docker pull ghcr.io/scaleapi/mcp-atlas:1.2.5
docker tag ghcr.io/scaleapi/mcp-atlas:1.2.5 agent-environment:latest
make -C mcp-atlas run-docker

# Option B — build from source
make -C mcp-atlas build && make -C mcp-atlas run-docker
```

Wait until logs show `Uvicorn running on http://0.0.0.0:19841`.  
Verify all servers are online:

```bash
curl -s http://localhost:19841/enabled-servers | jq -c
```

### 3. Start the completion service

```bash
make -C mcp-atlas run-mcp-completion
```

### 4. Run evaluation

```bash
cd task_generation

# Evaluate a model on all Confusion Trap tasks
uv run hallu-eval \
  --input final_tasks/confusion_tasks.jsonl \
  --model gpt-4o-2024-08-06 \
  --docker-snapshot

# Evaluate all four subtypes
for subtype in confusion memory reasoning void; do
  uv run hallu-eval \
    --input final_tasks/${subtype}_tasks.jsonl \
    --model gpt-4o-2024-08-06
done
```

### 5. Re-score from existing completions (no agent re-run)

```bash
uv run hallu-eval \
  --from-completion-csv results/<model>/confusion_tasks_completion.csv \
  --input final_tasks/confusion_tasks.jsonl \
  --output results/<model>/confusion_tasks.csv
```

---

## Scoring Methodology

Each task type uses a dedicated scoring strategy:

| Subtype | Strategy | Formula |
|---|---|---|
| **Confusion Trap** | `score_confusion_trap` | `correct_hit×0.20 + (1−forbidden_penalty)×0.30 + llm_score×0.50` |
| **Void Trap** | `score_void_trap` | `llm_score×0.60 + step_score×0.40` |
| **Memory Trap** | `score_parallel_execution` | `tool_coverage×0.10 + llm_score×0.80 + dep_score×0.10` |
| **Reasoning Trap** | `score_parallel_execution` | `tool_coverage×0.10 + llm_score×0.80 + dep_score×0.10` |

Stateful tasks (Memory/Reasoning with `strategy=state_check`) use Docker-based assertion execution:  
`assertion_score×0.50 + claim_score×0.40 + dep_score×0.10`

All LLM-judge prompts are defined in `task_generation/eval/scoring.py`.

---

## Task Format

Each JSONL line is a task object with the following schema:

```json
{
  "task_id": "confusion_001",
  "bucket": "ANALYTICS",
  "hallucination_type": "Confusion Trap",
  "difficulty": "Easy",
  "task": "<natural language instruction>",
  "available_tools": ["server_tool_a", "server_tool_b"],
  "ground_truth": {
    "strategy": "none|dynamic_script|state_check",
    "dynamic_reference_script": "<Python source>",
    "state_assertions": []
  },
  "evaluation_rules": {
    "required_tools": ["server_correct_tool"],
    "forbidden_tools": ["server_wrong_tool_a"]
  },
  "claims": [
    {"claim": 1, "description": "<what the agent must do>"}
  ],
  "should_stop_early": false
}
```

### Ground-truth strategies

| `strategy` | Used for | Description |
|---|---|---|
| `none` | Confusion / Void Trap | No pre-run needed; evaluation is purely judge-based |
| `dynamic_script` | Memory / Reasoning Trap (stateless) | `generate_reference_answer()` is executed; write ops are rolled back |
| `state_check` | Memory / Reasoning Trap (stateful) | State assertions are executed inside the Docker container |

---

## Reproducing Published Results

Pre-computed per-task JSON files for all 14 evaluated models are in `task_generation/results/<model>/task_results/`. Aggregate statistics are in `results/scores_summary.json`.

To recompute the summary from task files:

```bash
cd task_generation/results
python compute_scores.py
```

---

## Dataset Statistics

| Subtype | Count | Easy | Medium | Hard |
|---|---|---|---|---|
| Confusion Trap | 84 | 28 | 28 | 28 |
| Memory Trap | 82 | 27 | 28 | 27 |
| Reasoning Trap | 89 | 30 | 30 | 29 |
| Void Trap | 103 | 44 | 34 | 25 |
| **Total** | **358** | **129** | **120** | **109** |

---

## Infrastructure

MCPHallu builds on the [MCP-Atlas](https://github.com/scaleapi/mcp-atlas) infrastructure for running MCP servers in Docker. The `mcp-atlas/` subdirectory contains the agent environment (port 19841) and completion service (port 3001).

See `mcp-atlas/README.md` for detailed server setup instructions and API key configuration.

---

## License

This benchmark is released under the [MIT License](LICENSE).

The MCP-Atlas infrastructure (`mcp-atlas/`) retains its original license; see `mcp-atlas/LICENSE`.
