# 上下文治理修复后复测

2026-09-09；DeepSeek V4 Flash。与上一轮使用相同模型和预算配置、12 组固定合成历史、每组 raw/managed 各一次。只运行 context，没有运行 memory。原始测试状态依然放在历史反馈中，没有改成 Runtime 验证字段来绕过摘要忠实度检查。

被测版本为 `2d41fb1199e9023b0eaca7f0413e48d40269c3a2` 加本次未提交修改；修改快照保存于 `benchmarks/results/module-context-v3-20260909/runtime-changes.patch`。不能只用基础提交号代表此次实现。

| 指标 | raw | managed |
| --- | ---: | ---: |
| 六项事实全部答对 | 12/12 | 12/12 |
| 单字段正确 | 72/72 | 72/72 |
| 主请求平均实际输入 Token | 17,567 | 9,324.67 |
| 平均准备与回答耗时 | 2.315 秒 | 7.406 秒 |
| 摘要调用 | 0 | 6 |
| 含摘要的总输入 Token | 210,804 | 177,574 |
| 含摘要的总输出 Token（包含推理） | 3,040 | 11,678 |

主请求输入总量下降 46.92%。当前请求原文与工具配对两组均为 12/12。没有 Provider 重试或请求失败，usage 均完整。摘要增加输出与耗时，因此不能将主请求输入降幅当成总费用降幅。

旧失败案例 `48-1-1` 本轮六项全部正确。检查实际摘要后确认，它保留了 `unit_test=passed`、`integration_test=blocked_database` 和 `next_step=refund_integration`，并注明测试状态来自 Runtime 报告，未在本次对话重新验证。旧轮 managed 为 11/12、69/72，本轮为 12/12、72/72；不能从一次复测归因出各项修改的独立收益。

只有 6 组触发了摘要，其余 6 组验证的是无摘要的上下文路径。每个摘要场景只有一次压缩，不覆盖多次滚动更新的真实模型表现；本轮也不是自然代码任务完成率评测。通过这些样本不代表任意长历史都无损。

汇总、逐行结果、配置与修改快照：`benchmarks/results/module-context-v3-20260909/`。
完整输入、摘要 Session、Trace：`.tero/experiments/module-context-v3-20260909/`。

复现命令（需配置项目环境凭证，输出目录必须新建）：

```sh
python -m evaluations.module_benchmarks --suite context --output .tero/experiments/NEW_DIRECTORY
```
