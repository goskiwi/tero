# RepoMap：任务相关的代码导航

## 为什么保留

原 `pico-main` 已实现 Tree-sitter 符号图和任务相关排序，并有对应实验入口。
本次迁移保持解析关系、评分权重和排序算法，接入点只在 ContextManager，不依赖旧 Runtime。
参考实现：`/Users/yankai/Documents/Course/Agents/pico-main/pico/repo_map.py`。
既有实验入口：`scripts/day5_context_walkthrough.py` 的 `repo_map_experiment`（原项目）。

```text
当前请求＋本次已观察文件
→ 增量解析 Python
→ 构建静态关系图
→ 关键词相关度与个性化 PageRank
→ 多文件选择与 Token 预算
→ 文件路径、行号、符号签名
→ 模型用 read_file 查看实际内容
```

## 保留的算法

- Tree-sitter 提取函数、方法、类及模块。
- 推断调用、导入、继承、包含关系，以及测试名称对应关系。
- 词法评分作为 PageRank 的个性化分布。
- 最终分数沿用 `0.62 × lexical + 0.36 × graph + kind_boost`。
- 沿用反向边权重、多文件多样性选择、32 次迭代上限和 0.85 阻尼。
- 文件缓存沿用 mtime、ctime 和文件大小，不新增内容 hash 或持久索引。

静态关系使用名称和位置等启发式，不等于 Python 动态执行时的完整调用图。同名符号、动态
导入和运行时绑定可能有歧义。RepoMap 用来导航，不替代实际读取和验证。

## 在新骨架中的边界

默认开启。`--no-repo-map` 关闭，`--repo-map-tokens` 设置单独上限；同时受整包剩余预算约束。
没有可用符号、预算不足或索引失败时，正常 read/search 仍可工作。
保留原来的文件数量、大小、目录扫描和解析降级限制；扫描、解析和排名接入本次任务的取消/时间预算。
取消和超时向外传播，不伪装成普通索引失败。

诊断信息只进入 `repo_map_built` Trace：图节点/边、选中符号、词法与图分数、缓存命中和解析情况。
模型只得到导航文本。已有实验可继续使用 `RepoMap.query/render` 和 `details` 进行比较。

本次没有重新运行旧实验、测试或真实模型任务。原算法实验结果不能直接当作新版本任务成功率
或性能提升；需要后续实际对照才能写相应指标。
