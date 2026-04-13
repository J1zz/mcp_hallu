# MCP 幻觉评测设计文档

## 回滚触发时机

回滚代码在 `run_gt_execution.py` 的 `run_one_gt_script()` 函数中，有 **3 个触发点**，均通过 `finally` 块保证必然执行：

1. `exec(script_src)` 加载脚本本身抛异常 → 立即回滚
2. `generate_reference_answer` 函数找不到或不可调用 → 立即回滚
3. `generate_reference_answer()` 正常/异常执行完毕 → `finally` 块中回滚

> 只要脚本开始执行，回滚 **100%** 会在任务结束时触发，无论成功还是失败。
> 使用 `--no-rollback` 参数可强制禁用。

---

## A 类：已注册，可自动回滚

| 工具 | 逆操作 | 回滚所需信息来源 |
|---|---|---|
| `airtable_create_record` | `airtable_delete_record` | 结果返回 `id` |
| `mongodb_insert-many` | `mongodb_delete-many`（按插入的 `_id` 过滤） | 结果返回 `insertedIds` |
| `mongodb_create-collection` | `mongodb_drop-collection` | args 中的库名/集合名 |
| `mongodb_rename-collection` | `mongodb_rename-collection`（名字互换） | args 中的新旧名 |
| `notion_API-post-page` | `notion_API-delete-a-block` | 结果返回 `id` |
| `notion_API-create-a-database` | `notion_API-delete-a-block` | 结果返回 `id` |
| `google-workspace_create_event` | `google-workspace_delete_event` | 结果返回 `id` |
| `lara-translate_create_memory` | `lara-translate_delete_memory` | 结果返回 `id` |
| `lara-translate_add_translation` | `lara-translate_delete_translation` | 结果返回 `id` |

---

## B 类：未注册，但可补充注册（低成本）

只需在 `COMPENSATION_MAP` 中补几行 lambda 即可覆盖：

| 工具 | 逆操作 | 回滚所需信息来源 |
|---|---|---|
| `memory_create_entities` | `memory_delete_entities` | 实体名在 args 中 |
| `memory_create_relations` | `memory_delete_relations` | relation 信息在 args 中 |
| `memory_add_observations` | `memory_delete_observations` | entity 名 + obs 内容在 args 中 |
| `notion_API-patch-block-children` | `notion_API-delete-a-block` | 子块 id 在结果中 |
| `filesystem_move_file` | `filesystem_move_file`（src/dst 互换） | args 中有 src/dst |
| `desktop-commander_move_file` | `desktop-commander_move_file`（src/dst 互换） | args 中有 src/dst |

---

## C 类：未注册，理论可逆但拿不到足够回滚信息

需要在调用前先读取"修改前的快照"才能还原，现有机制不支持：

| 工具 | 无法自动回滚的原因 |
|---|---|
| `airtable_update_record` | 需要修改前的字段值快照，执行前未记录 |
| `airtable_update_field` | 需要修改前的 schema 快照 |
| `airtable_update_table` | 需要修改前的表结构快照 |
| `mongodb_update-many` | 需要被修改的每条文档修改前的值 |
| `notion_API-patch-page` | 需要修改前的 properties 快照 |
| `notion_API-update-a-block` | 需要修改前的块内容快照 |
| `google-workspace_update_event` | 需要修改前的事件详情快照 |
| `lara-translate_update_memory` | 需要旧内容快照才能还原 |
| `github_update_issue` | 需要 issue 修改前的内容快照 |

---

## D 类：未注册，工具集中缺少对应的删除/撤销工具

| 工具 | 理论逆操作 | 缺失原因 |
|---|---|---|
| `airtable_create_table` | `delete_table` | 工具集中无 delete_table |
| `airtable_create_field` | `delete_field` | 工具集中无 delete_field |
| `mongodb_create-index` | `drop_index` | 工具集中无 drop_index 工具 |
| `notion_API-create-a-comment` | `delete_comment` | 工具集中无 delete_comment |
| `github_create_issue` | `delete_issue` / `close_issue` | close ≠ 删除，且无法真正撤销创建 |
| `github_create_repository` | `delete_repository` | 工具集中无 delete_repository |
| `filesystem_create_directory` | `rmdir` | 工具集中无对应删除目录工具 |
| `desktop-commander_create_directory` | `rmdir` | 同上 |

---

## E 类：本身不可逆，禁止在 GT 脚本中使用

**任务设计时必须避免在 `dynamic_reference_script` 中调用以下工具：**

| 工具 | 不可逆原因 |
|---|---|
| `github_push_files` | Push 到远程 Git 历史永久存在，revert 会产生新 commit，不是真正撤销 |
| `github_merge_pull_request` | Merge 无法 unmerge，只能再开 PR revert |
| `git_git_commit` | 本地 commit 需要提前记录 HEAD hash 才能 reset，无法提前知道 |
| `google-workspace_send_email` | 邮件发出后 API 不支持 unsend |
| `slack_conversations_add_message` | 发出消息需要 `ts` 时间戳才能删除，且删除≠撤回 |
| `mcp-code-executor_execute_code` | 执行任意代码，副作用完全不可预测 |
| `mcp-code-executor_execute_code_file` | 同上 |
| `mcp-code-executor_install_dependencies` | 包安装到环境，无对应 uninstall 工具 |
| `mcp-server-code-runner_run-code` | 任意代码执行，副作用不可预测 |
| `github_update_pull_request_branch` | 分支更新无法自动撤销 |

---

## 文件系统操作说明

文件系统写操作在沙箱（`/data` 目录）中影响范围可控，但目前均未注册回滚：

| 工具 | 现状 | 建议 |
|---|---|---|
| `filesystem_write_file` | 未注册 | GT 脚本中只写新文件，任务完成后人工清理或忽略 |
| `filesystem_edit_file` | 未注册 | 需要原内容快照，建议 GT 脚本避免使用 |
| `desktop-commander_write_file` | 未注册 | 同上 |
| `desktop-commander_edit_block` | 未注册 | 同上 |
| `filesystem_create_directory` | 未注册 | 沙箱隔离，影响可控 |
| `desktop-commander_create_directory` | 未注册 | 同上 |

---

---

# Eval 评分设计

## 总体路由逻辑

评分入口为 `score_task()`，按幻觉类型 + bucket + strategy 三层路由：

```
Confusion Trap  →  score_confusion_trap()        （纯工具名规则）
Void Trap       →  score_void_trap()             （越早停止越高分）
Memory / Reasoning Trap：
  bucket ∈ {PRODUCTIVITY, CODING} && strategy == "state_check"
              →  score_state_assertions()         （断言验证世界状态）
  其他情况    →  score_parallel_execution()       （LLM 对比 GT 日志）
```

---

## 一、Confusion Trap（工具混淆）

**错误类型**：Agent 在多个语义相似的工具中选错了工具。正确工具和错误工具都能执行，但错误工具的结果不满足任务要求（格式不符、字段缺失等）。

**评分函数**：`score_confusion_trap()`

**评分维度**：

| 维度 | 权重 | 说明 |
|---|---|---|
| 正确工具命中 | 50% | `evaluation_rules.correct_tool` 是否被调用 |
| 禁用工具未调用 | 30% | `evaluation_rules.forbidden_tools` 中的工具是否全部回避 |
| 语义关键词匹配 | 20% | Agent 回答中是否提及 claims 关键词 |

**关键字段**：
- `evaluation_rules.correct_tool`：唯一正确工具名
- `evaluation_rules.forbidden_tools`：所有应当避免的混淆工具
- `claims`：描述混淆点的自然语言步骤，用于关键词匹配

---

## 二、Void Trap（不可达）

**错误类型**：任务看似合理，但工具集中根本没有能完成该任务的能力（缺失工具、缺失参数、超出约束）。Agent 应当尽早识别并停止，而不是反复尝试或编造结果。

**评分函数**：`score_void_trap()`，支持两种模式：

| 模式 | 逻辑 |
|---|---|
| `step_based`（默认） | 按实际步骤数递减打分；步骤越少（越早放弃）得分越高 |
| `llm_claims` | 调用 LLM，依据 claims 描述的"不可能点"判断 Agent 是否正确识别了限制 |

**关键字段**：
- `should_stop_early: true`（所有 Void Trap 任务必须设置）
- `claims[].expected_failure`：描述 Agent 应识别的不可达原因
- `claims[].discovery_method`：Agent 应通过哪种方式发现限制

---

## 三、Memory Trap（记忆/信号追踪）

**错误类型**：Agent 需要在长链工具调用中从早期的噪声输出里提取关键信号，并在后续步骤中正确使用。错误表现为：信号提取错误、信号丢失、在后续步骤中用了错误的值。

### 3a. 无状态（bucket ∉ STATEFUL_BUCKETS 或 strategy = dynamic_script）

**评分函数**：`score_parallel_execution()`

**评分流程**：
1. 执行 GT 的 `dynamic_reference_script`，获得完整执行日志（含信号提取过程和最终结果）
2. 将 GT 日志 + Agent 日志一起交给 LLM 打分（0-1 分）
3. 辅以 dependency 有向图验证（验证步骤调用顺序是否符合 claims 中的 `dependency_on_step`）

**关键字段**：
- `ground_truth.dynamic_reference_script`：可执行 Python 函数，返回完整执行记录字符串
- `claims[].signal_to_remember`：标注哪一步提取了信号
- `claims[].uses_signal`：标注哪一步使用了该信号
- `claims[].dependency_on_step`：构建依赖有向图，验证调用顺序

### 3b. 有状态（bucket ∈ {PRODUCTIVITY, CODING} 且 strategy = state_check）

**评分函数**：`score_state_assertions()`

**评分流程**：
1. 逐条 `eval()` 执行 `state_assertions` 中的断言表达式
2. 统计通过率作为断言得分（90%权重）
3. 辅以 dependency 有向图验证（10%权重）

**关键字段**：
- `ground_truth.state_assertions`：断言数组，每条形如：
  ```json
  {
    "description": "人类可读描述",
    "code": "os.path.exists('/data/output.txt')",
    "expected": true
  }
  ```
  `code` 为纯 Python 表达式，`eval()` 可用变量：`os`、`json`、`re`
- 断言应覆盖：文件/目录存在性、内容正确性、副作用验证（至少 2-3 条）

**退化规则**：`state_assertions` 为空或全部 exec_error 时，fallback 到工具覆盖率评分

---

## 四、Reasoning Trap（推理/分支）

**错误类型**：Agent 需要根据工具返回值做条件判断，选择正确的分支工具链执行。错误表现为：进入了错误的分支、忽略了条件判断、聚合了错误分支的结果。

### 4a. 无状态（bucket ∉ STATEFUL_BUCKETS 或 strategy = dynamic_script）

**评分函数**：`score_parallel_execution()`（同 Memory Trap 无状态路径）

**关键字段**：
- `ground_truth.dynamic_reference_script`：包含完整条件分支逻辑的 Python 函数
- `claims[].branch`：标注该 step 属于哪个分支（`"branch_a"` / `"branch_b"` 等）
- `claims[].dependency_on_step`：用于验证分支选择顺序

**branch 验证逻辑**（`_score_branch_selection()`）：
1. 从 GT 日志中提取实际走过的分支 ID（通过 `Branch X triggered` 关键词）
2. 从 claims 中找到该分支对应的 `required_tool`
3. 验证 Agent 是否调用了该工具，未调用则判为进错分支

### 4b. 有状态（bucket ∈ {PRODUCTIVITY, CODING} 且 strategy = state_check）

**评分函数**：`score_state_assertions()`

**断言设计建议**（针对分支验证）：
- 正向断言：正确分支产生的文件/状态应存在（`expected: true`）
- 反向断言：错误分支的特征文件不应存在（`expected: false`）
- 内容断言：正确分支输出的内容包含期望关键词

```json
"state_assertions": [
  {"description": "Correct branch output exists", "code": "os.path.exists('/data/branch_a.txt')", "expected": true},
  {"description": "Wrong branch NOT taken", "code": "os.path.exists('/data/branch_b.txt')", "expected": false},
  {"description": "Result contains expected value", "code": "'approved' in open('/data/branch_a.txt').read()", "expected": true}
]
```

---

## 五、Dependency 验证（跨类型通用）

**函数**：`_validate_dependency_order()`

所有幻觉类型（Confusion 除外）的评分中，都会附加 dependency 验证作为辅助分：

1. 解析 `claims` 中每个 step 的 `dependency_on_step` 字段，构建有向图
2. 对 Agent 工具调用序列，检查"B 依赖 A"时，A 对应工具是否在 B 之前出现
3. 有违规则扣分，无依赖关系则满分

**权重**：在 `score_state_assertions()` 中占 10%，在 `score_parallel_execution()` 中作为 LLM 评分的补充项
