# 应用与评测

## Coding 应用

在可信仓库中执行一个修复需求，Runtime 负责工具执行与验证，应用负责交付展示：

```bash
uv run tero code --cwd /path/to/repo \
  --verify 'python -m pytest -q' '修复订单数量大于一时总价错误，保留测试'
```

默认 code 模式在执行修改与命令前请求批准。显式传入 `--mode auto` 才自动执行。
应用要求验证命令，但不自动发现或更改项目测试。会保存：

- `.tero/runs/<run_id>/report.json`：Runtime 结果与指标。
- 同目录 `delivery.json`、`delivery.md`：回答、验证状态、轮数、工具数、指标、工作区差异。

差异来自当前工作区对 HEAD 的 Git diff，包括 staged 与 unstaged 内容，可能含用户已有修改；
另列 untracked 文件名，不包含其内容。不建立 Agent 变更归属，不自动 add、commit 或 push。
Git 不可用、仓库没有 HEAD、输出截断或观察超时时，报告实际限制；这些展示问题不改写任务结果。
Git 展示共享任务剩余预算，报告文件仍会保存。这里没有迁移旧 CodingWorkflow 接口。

## 可选评测

**以下入口会调用真实模型并执行验证命令，使用 API 额度。实际执行记录见 [DeepSeek 评测](deepseek-evaluation.md)。**
每次运行创建新的临时仓库目录，保留目录便于检查，不覆盖用户提供的仓库。

```bash
uv run tero eval --case pricing
uv run tero eval --case compaction
uv run tero eval --case resume
uv run tero eval --case approval
uv run tero eval --case revision
```

| 场景 | 观察什么 |
| --- | --- |
| pricing | 跨文件定位计价与展示调用，修复后原始验收检查通过 |
| compaction | 人工构造历史压力，实际触发压缩后仍满足文本处理约束 |
| resume | 从有未落盘工具结果的 Session 恢复，不重放旧写入，再修复计价 |
| approval | 拒绝修改与验证审批，已有样例文件保持不变且不报告完成 |
| revision | 读取计价文件后注入外部注释，修复完成时仍保留外部修改 |

案例改编自原项目的计价、压缩与工具边界场景，使用新版 API，不读取旧 RunLog 或成绩。
评测另建临时目录，使用原始 checks.py 验收交付代码，并核对模型工作区的 checks.py 没有变化。
临时目录不是沙箱，执行模型修改的代码仍要求信任环境。审批场景只检查样例文件与拒绝结果，
不能证明任意工作区都不存在外部副作用。压缩场景属于受控实验，不是自然长任务成功率。

加 `--no-repo-map` 可在同一场景对照；比较时保持模型、需求和其他配置一致，多次重复后再汇总。
单次通过不代表稳定收益。结果保存为临时工作区 `.tero/evaluation.json`，包括真实验收输出。

## 指标口径

`result.metrics` 包含模型请求数、完成响应数、重试数、压缩次数、Token 和耗时。
Token 来自后端 usage；缺失值为 null，缺少任一用量字段或存在未完成请求时 `usage_complete=false`。
部分响应提供的用量仍保留，但不能当成本次全部消费。输入、输出及缓存 Token 单独累计，
缓存 Token 是输入的子集，不再额外相加。上下文预算估算与实际 usage 分开记录。
本 Run 包含记忆与压缩辅助请求，不包含独立子任务；子任务有自己的报告。

模型请求最多三次尝试，退避等待也共享单次请求和任务剩余时间。只重试暂时性服务／网络错误；
上下文超限进入压缩流程，永久请求错误不重试。工具不参与 Provider 重试。
显示端的断管／关闭异常只关闭显示回调；持久化错误不会静默忽略。

## 正常窗口与压缩实验

DeepSeek 官方规格为 1M 上下文、最大 384K 输出；GPT-5.6 Sol/Luna 为 1,050,000 上下文、
最大 128K 输出。模型规格不等于每次请求必须使用的额度。当前 `.env.local` 配置 DeepSeek
1,000,000 上下文与 32,000 输出，CLI 显式参数优先于环境配置。

压缩实验人为使用 64,000 上下文、16,384 主请求输出，构造 100 条历史观察来触发压缩。
摘要正文限制 2,000 token；生成上限按独立 32,768 配置与实验窗口空间计算（该场景为 32,000），
计入输入分块预算。辅助 JSON 请求采用 low 推理强度。正常摘要正文默认 4,096 token。
这验证压缩后继续执行的链路，不代表已经测试完整 1M 长上下文。

来源：[DeepSeek 规格](https://api-docs.deepseek.com/quick_start/pricing)、
[DeepSeek Responses 参数](https://api-docs.deepseek.com/api/create-response/)、
[GPT-5.6 Luna](https://developers.openai.com/api/docs/models/gpt-5.6-luna)。

## 长任务与提前压缩对照

```bash
python -m evaluations.long_task
python -m evaluations.long_task --compaction-trigger-tokens 24000
```

两组同用正常 1M 模型窗口、32K 主输出、64 轮和 900 秒总预算，关闭长期记忆以聚焦代码执行与上下文。
任务包括订单计算、库存、重复请求、取消、持久化、CLI 与文档，使用 25 个原始验收测试。
压缩组在估算输入达到 24K 时尝试提前压缩，不缩小模型窗口或摘要请求窗口。
该阈值不包含输出预留；模型硬窗口校验仍独立执行。无法压缩最近完整交互而硬窗口仍足够时，
继续保留该交互，不拆散工具调用与结果。阈值因此不是强制请求长度上限。
运行中逐次观察工具配对和原始请求是否保留；压缩组没有实际压缩即不能算此专项通过。

## 当前修复的验证状态

Session 已更新为 v5：已观察边界与压缩失败记录持久化；旧格式直接拒绝。
上下文摘要现在使用普通 Markdown 请求，长期记忆仍使用 JSON。摘要失败分类及候选保留到 Trace，
同条件不无条件自动重试；手动入口为 `/compact` 或 `--resume ID --compact`。
本文历史实验属于修复前版本。本次按用户要求未运行测试／真实模型，不能沿用旧通过数量证明修复有效。
