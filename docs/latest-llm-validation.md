# 最新版本：恢复治理与真实长任务验证

日期：2026-09-09。模型：DeepSeek V4 Flash，官方 Responses API。代码包含最近的失败计数修复。

本地完整回归已在前一步通过：82 项。本轮补充真实模型执行，不把未触发的治理分支算作真实模型覆盖。

| 场景 | 实际结果 | 轮数 | 工具数 | 耗时 |
| --- | --- | ---: | ---: | ---: |
| approval | 达到验收条件 | 4 | 6 | 13.789s |
| revision | 达到验收条件 | 8 | 11 | 12.055s |
| resume | 达到验收条件 | 8 | 11 | 14.019s |
| 长任务压缩 | 通过 | 37 | 54 | 400.467s |

## 实际覆盖

- 审批：发生一次 approval_denied，最终 verification_denied 安全停止，样例文件保持不变；模型没有重复请求同一已拒绝操作，因此本次没有触发拒绝缓存分支。
- 版本变化：真实触发 revision_conflict，并经历一次 command_failed；最后通过原始验收并保留外部注释。
- 中断恢复：补记 interrupted，读取确认 missing_path，没有重放旧写入，最终完成修复和验收。
- 以上失败结果均包含 recovery 指引。重复三次停止、拒绝缓存、成功后重置计数等分支，由前一步的确定性本地回归覆盖，不声称在本轮真实模型中全部触发。
- 长任务：1M 窗口、24K 提前阈值；6 次压缩成功，37 次主请求均保留原需求和完整工具配对，Runtime 验证与原始 25 项独立验收通过。测试和 AGENTS.md 未修改。

长任务有 3 次 Provider 重试；已返回的 usage 仅是部分用量，usage_complete=false，不能当成完整计费总额。单次成功不等于稳定成功率，也没有完整 1M 输入压力结论。

## 原始证据

- approval: `.tero/evaluations/latest-governance/5849692fa5c94f1e/evaluation.json`
- revision: `.tero/evaluations/latest-governance/bc01afca95004e7f/evaluation.json`
- resume: `.tero/evaluations/latest-governance/6cf30c290963459d/evaluation.json`
- 长任务：`.tero/evaluations/archive/ab2bde329b634891/evaluation.json`，同目录保留 Session、Trace 与交付报告。
