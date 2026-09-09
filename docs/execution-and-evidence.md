# 执行阶段、结果留存与只读并行

本次恢复原版已有的实用能力，继续采用单写者 Session 快照，只加载根目录 AGENTS.md。

## 一次调用

保存完整模型回复，每个调用阶段为 pending；逐个准入。拒绝直接保存 finished 和结果。
通过准入后，先持久化 running，再执行；结果与 finished 一起保存。恢复不重放旧调用：

- pending：记录 not_started、无副作用。
- running 的读取：记录中断，可重新读取。
- running 的文件修改：有收据时对照当前文件；记录与 before/after 状态的关系，不伪造执行成功或作者归属。
- running 的 Shell：不能完整确认外部影响，保留 unknown。
- finished：使用已保存结果。

running 落盘后、操作开始前仍有崩溃窗口；这时按不确定状态处理，不声称恰好执行一次。
Session 不使用版本标签；加载时校验实际字段、执行状态和工作区归属，不提供迁移分支。

## 结果保存与上下文预览

ToolExecutor.finish 是主线程的结果边界：脱敏、必要时保存 Artifact、生成预览、更新读取版本、写 Trace。
模型预览上限 12 KiB；大结果或已截断捕获带 artifact_id，read_artifact 按偏移读取，每页最多 8 KiB。
分页读取的是本 Session 已保存的内容，不重新执行原工具。UTF-8 分页不会拆断字符。

Shell stdout/stderr 分别保留最多 1 MiB 的首尾内容，记录 total_bytes、retained_bytes 和截断状态。
单份文本 Artifact 最多保留约 1 MiB，超限保留首尾并标记中间未保存。字段含义：

- capture_truncated：保留的结果不是完整捕获，已丢弃部分无法由分页恢复。
- projection_truncated：当前模型只看到预览或某页，更多已保存内容可按 ID 查看。

旧的大读取结果在主上下文中可替换为 Artifact 引用，近期结果、尚未观察尾部和失败证据保留。
这一步不需要调用摘要模型，也不修改原始 Session；确实仍需压缩时才生成摘要。

目录 list_files 使用 offset / limit / next_offset；目录发生变化应从零重新列举。
read_file 仍明确返回读取范围和总行数，不把读到的一个范围当成整个文件。

## 文件前像、修改收据与 Diff

文件工具修改前保存原始字节前像（包括已有未提交内容），随后保存 prepared 收据，再提交文件。
收据包含文件路径、前后版本、原权限和前像 ID，成功后记录 applied 及单次 Diff ID。
保存前像失败不会进入文件替换。前像没有为保持原始字节而脱敏，只存于私有状态目录（文件权限 600），
read_artifact 不允许模型读取 preimage；它不是自动回滚入口。

任务 Diff 从每个文件首次前像与当前磁盘内容生成：A→B→A 没有净内容差异。
若修改链断开或当前状态与最后收据不一致，明确标为可能包含外部变化。
纯 Shell 修改没有自动全工作区备份，不冒称拥有它的修改前像；报告保留观察路径与覆盖限制。
交付报告同时提供任务 Diff 链接与 Git 工作区视图，后者可能包含用户原有修改，不用于证明 Agent 独有归属。

数据位置：

```text
.tero/
  sessions/<session_id>.json
  artifacts/<session_id>/<id>.json   # 描述信息
  artifacts/<session_id>/<id>.txt    # 脱敏结果或 Diff
  artifacts/<session_id>/<id>.bin    # 私有原始文件前像
  runs/<run_id>/report.json
  runs/<run_id>/delivery.md
```

## 并行和取消

模型请求开启 parallel_tool_calls；Runtime 只把连续的只读调用分为最多 4 个一组。
read/search/list/read_artifact 可并行；edit/write/Shell/delegate 都是串行边界。
线程只运行已准入读取，Session、Artifact、版本缓存和 Trace 更新由主线程顺序完成。
默认 --max-parallel-tools 4；可显式设置为 1 使用串行。线程池不是一致性文件系统快照，外部编辑仍需版本复查。

审批处 Ctrl+C 传播为任务取消，不再被当成普通拒绝。模型传输监听取消，主动关闭已连接 socket，
包括等待响应头和等待下一块流数据的情况。连接建立前的 DNS/系统调用仍受网络超时控制，不声称硬实时。

## 验证范围

本地回归包含真实临时文件、真实 Shell、大结果分页、目录分页、写入切点模拟中断、
真实本地 HTTP 静默响应取消、只读并行屏障和主线程状态更新检查。
本轮未调用付费真实 LLM，之前的长任务报告属于旧格式运行记录，不能直接当成本次重构的全链路成绩。

一次小型热缓存读取测量（4 个约 4 MB 文件，读取 10 行并计算全文版本，5 次测量中位数）：
串行 47.87 ms，四路并行 45.11 ms。仅说明该本地样例，不能推导整体 Agent 加速或稳定性能收益。

后续已完成串行配置下的真实模型验证，见 [最终验证](interview-final-validation.md)。上文未调用付费模型指本轮核心实现阶段。
