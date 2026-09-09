# Tero

一个便于阅读和讲解的本地 Agent Harness。只使用原生 Responses Tool Calling，主线是：

```text
加载 Session → 召回长期记忆 → 准备上下文 → 请求模型
                                        ↓
                        工具调用 → 执行并保存 → 下一轮
                        最终文本 → 测试验证 → 完成或继续修复
                                        ↓
                              保存会话 → 更新长期记忆
```

这是对原版 Pico 的重新组织，不兼容旧 Session、XML 工具协议、LayeredMemory 或旧命令行参数。
旧项目没有被覆盖。本目录不存在旧实现的兼容包装。

## 安装与启动

需要 Python 3.11+ 和 ripgrep（`rg`）；命令工具的进程组终止支持 macOS/Linux。Git 可选，缺失时仅提供文件系统工作区能力。

```bash
uv sync --extra dev
uv run tero --cwd /path/to/trusted/repo --trace "解释这个项目的入口"
uv run tero --cwd /path/to/trusted/repo --mode auto \
  --verify 'python -m pytest -q' "修复计价问题，保持测试不变"
uv run tero --cwd /path/to/trusted/repo --resume latest "继续上次任务"
```

`--verify` 是你指定的验证命令。配置与执行记录分别展示；尚无验证执行时状态为 `not_run`，不宣称已经验证。历史结果保留其原命令，不能代替本轮验证。
上面列的是使用方式，不代表已经执行过这些验证。

配置使用 `.env` / `.env.local` 或环境变量：

```dotenv
TERO_OPENAI_API_KEY=your-key
TERO_OPENAI_API_BASE=https://api.deepseek.com
TERO_OPENAI_MODEL=deepseek-v4-flash
TERO_CONTEXT_TOKENS=1000000
TERO_OUTPUT_TOKENS=32000
```

环境变量优先；本地 `.env.local` 覆盖 `.env` 的文件值。`--config-dir` 可以指定配置目录。
本地 `.env.local` 已配置 DeepSeek 官方接口，权限为 600，并被 Git 忽略。
模型名称和服务地址可通过 `--model`、`--base-url` 覆盖。没有旧提供商协议回退。

## Coding 场景与评测入口

`uv run tero code --cwd /path/to/repo --verify 'python -m pytest -q' '修复需求'`
提供需求到验证、交付报告的应用流程，不自动提交代码。
`uv run tero eval --help` 查看五个独立评测场景；实际运行会调用模型并执行命令。
安装后对应命令为 `tero code` 和 `tero eval`。
详见 [应用与评测说明](docs/applications-and-evaluation.md)。实际执行结果见 [DeepSeek 评测记录](docs/deepseek-evaluation.md)。

## 核心能力

- **工具执行**：`list_files`、`read_file`、`search`、`edit_file`、`write_file`、`run_shell`、`read_artifact` 和只读 `delegate`。
- **恢复指引**：区分失败原因、恢复条件和具体动作；版本冲突返回预期／实际版本及建议读取参数，建议不自动执行。
- **编辑诊断**：匹配失败返回位置或建议读取范围，仅提供定位提示，不执行模糊替换。
- **请求重试**：暂时性模型服务／网络错误最多三次尝试，共享请求时间预算，不重放工具。
- **指标与交付**：汇总后端实际 usage、轮数、工具数、压缩与耗时；缺失用量明确标记。
- **文件保护**：Runtime 记录已读版本；edit 和覆盖写均需先读；唯一匹配、保留换行、提交前复查、原子发布。
- **结果与修改证据**：大结果先保存再预览，按 ID 分页读取；文件修改保存原始前像、收据和任务净 Diff，不自动回滚。
- **只读并行**：默认最多 4 个只读并行；可设置为 1 使用串行，修改与 Shell 始终串行，Session 和结果记账由主线程完成。
- **运行预算**：默认 32 个主模型轮次、600 秒总时间、120 秒单命令上限。子任务和辅助模型请求共享剩余时间。
- **上下文**：计入指令、工具和消息，预留输出额度；完整交互作为历史裁剪单位。模型 tokenizer 不匹配时使用明确标记的估算。
- **RepoMap**：Tree-sitter 提取 Python 符号与静态关系，结合关键词相关度与个性化 PageRank 排序；在剩余预算内提供代码导航，实际细节仍由 read/search 获取。
- **压缩**：直接生成 Markdown 交接摘要，提示词建议六段组织但不逐字校验标题；保护当前需求、近期完整工具交互及尚未被主模型观察的新消息。
- **长期记忆**：模型选择相关条目；任务结束后从本次用户消息提取 add/update/delete；Python 校验并保存。没有独立后台 Agent。
- **恢复**：加载 Session，补记没有结果的调用为中断/未知。文件已读版本重新建立；不会自动重放旧命令。
- **完成判定**：最终回答触发已配置验证；验证失败反馈模型修复，验证后的文件变化使本次结果失效。未确认的副作用先处理，再允许完成。

## 阅读顺序

1. [`agent_loop.py`](tero/agent_loop.py)：先完整看一次任务如何运行。
   完成分支进入 [`completion.py`](tero/completion.py)，集中处理副作用、验证和最终文件复查。
2. [`session.py`](tero/session.py)：保存哪些事实，恢复时处理什么。
3. [`tool_executor.py`](tero/tool_executor.py)：工具如何校验、执行和反馈。
4. [`context.py`](tero/context.py)：什么保留、什么压缩、预算怎样计算。
5. [`memory.py`](tero/memory.py)：长期信息如何选择与更新。
6. [`provider.py`](tero/provider.py)：最后再看 HTTP/SSE 细节。

需要讲解代码检索时，再读 [`repo_map.py`](tero/repo_map.py) 和 [RepoMap 说明](docs/repomap.md)。

[`docs/design.md`](docs/design.md) 说明状态归属与重要边界。

## CLI

- `--mode ask`：只有读取工具，不执行验证命令。
- `--mode code`：文件修改、Shell、验证执行前批准，默认模式。
- `--mode auto`：用户明确允许自动执行操作。
- `--workspace-root path`：显式固定工作区根；未指定时从启动目录发现 Git 根，非 Git 目录使用启动目录。
- `--allow-tool name`：可重复指定工具白名单，与模式和子代理权限取交集。
- `--allow-write path`：限定可修改文件；通用 Shell 在这种配置下禁用，因为 cwd 不能限制 Shell 写入路径。
- `--max-turns`、`--max-seconds`：本次请求的预算。
- `--context-tokens`、`--output-tokens`：实际后端窗口与输出预留。
- `--no-memory`：关闭自动召回和提取。
- `--no-repo-map`：关闭代码导航，便于与纯 read/search 流程对照；`--repo-map-tokens` 调整导航预算（默认 1200）。
- 交互命令：`/session`、`/memory`、`/forget ID`、`/effects`、`/compact`、`/retry-denied`、`/reset`、`/exit`。

`/effects` 查看未确认影响及后续工具观察的历史引用。文件编辑结果未知时，重新读取对应文件；
Shell 或验证命令影响未知时，反馈模型检查相关文件、差异、进程或远程状态，不要求通用人工解锁。
Runtime 记录实际成功的观察调用；这只是检查证据，不判断其语义是否足够，也不清除未知的外部影响。
局部检查和验证通过后可结束本地任务，最终回答与报告仍保留未确认影响。
涉及无法核实的远程写入或发布结果时，模型应解释具体阻塞并请求用户帮助；没有自动风险分类器。
需要验证的未完成任务恢复后仍需配置 `--verify`，不能切换 ask 模式绕过。

`/compact` 显式重试摘要；非交互模式使用 `--resume SESSION_ID --compact`。
提前压缩失败时保留原历史，记录原因，并跳过相同摘要来源与配置的自动重试。
用户主动重试或实质输入／预算／模型路由变化后重新评估；真正超出窗口不能强行发送。

`/retry-denied` 或 `--resume ID --retry-denied` 允许用户显式重新申请已拒绝的审批，
不会自动批准或执行操作。重复失败记录随未完成任务恢复；新建 Session 或完成后开始新任务才重新计数。

Session 不使用版本标签；加载时校验实际字段、执行状态和工作区归属，不提供迁移分支。

`/reset` 新建 Session，长期记忆保留。`/forget` 删除长期记忆，历史记录不同时删除。

## 数据与边界

```text
workspace/.tero/
  sessions/<id>.json       # 唯一会话恢复状态
  memory.json              # 项目级长期记忆，独立于会话恢复
  runs/<id>/trace.jsonl     # 诊断，不驱动任务状态
  runs/<id>/report.json    # 本次结果
```

只运行用户信任的本地仓库。工作目录不是沙箱；Shell 和测试可以访问网络及工作区之外的资源。
工作区观察忽略 `.git`、`.tero`、依赖和缓存目录，只报告可见文件变化，不是外部副作用审计。
原子替换避免半写入文件；版本复查不是跨进程原子 CAS。一个 Session 按单进程顺序执行，
不支持多个进程同时写同一个 Session，也没有 Worktree 合入、自动文件回滚或精确进程恢复。

根目录 `AGENTS.md` 每轮读取并完整计入预算；本版没有嵌套目录规则发现系统。
原始模型输出和工具参数保存在 Session，可能包含用户提供的敏感内容，不保证它适合公开。
已配置的 API Key 等环境值会在工具文本、Trace 和报告中脱敏；不声称实现通用秘密识别。

## 验证状态

当前上下文修复按用户要求仅做静态检查，尚未运行测试或真实模型复测。此前版本的本地测试与 DeepSeek 评测结果见 [评测记录](docs/deepseek-evaluation.md)。
测试命令为 `uv run pytest -q`；历史结果不替代修改后的重新验证。

Responses 协议参考：[OpenAI Function Calling 文档](https://developers.openai.com/api/docs/guides/function-calling)。

最新真实长任务压缩复测：Session v7 完成 4 次压缩并通过原始 25 项独立验收，见 [报告](docs/session-v7-compaction-retest.md)。这是单案例结果，完整本地回归尚未在本轮重跑。

当前版本完整回归：82 项通过，静态、格式与 CLI 帮助检查通过；审查与小修复见 [记录](docs/current-version-review.md)。本轮没有重新调用真实模型。

最近代码修复后的真实 LLM 验证：恢复、审批、版本变化场景通过，长任务完成 6 次压缩并通过 25 项独立验收。详见 [最新验证记录](docs/latest-llm-validation.md)，其中区分本地回归与真实模型实际覆盖的分支。

## Tero 命名

项目目录为 `Tero/`，Python 包为 `tero`，公开类为 `Tero`。
命令为 `tero`、`tero code`、`tero eval`；配置使用 `TERO_*`，运行数据写入 `.tero/`。
不保留旧包、旧命令、旧环境变量或旧 Session 格式的兼容入口。
既有评测原始记录归档在 `.tero/evaluations/`，其中原运行名称与临时路径保持不变，作为历史证据；
文档中的原 `pico-main` 来源路径也保留。名称变更后的回归与入口检查不等于重新运行这些真实 LLM 实验。

执行阶段、结果留存、前像与并行的具体边界见 [实现说明](docs/execution-and-evidence.md)。本次升级使用 Session v8，不兼容旧状态。

面试版本已完成一次串行配置下的真实模型验证，当前默认恢复为最多 4 个只读并行。历史结果见 [最终验证](docs/interview-final-validation.md)。
