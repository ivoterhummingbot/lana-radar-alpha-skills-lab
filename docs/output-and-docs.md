# Output 与文档目录说明

本项目区分“可提交的研究代码/文档”和“本地生成的结果 artifact”。

## `docs/`

`docs/` 存放可以提交到 GitHub 的长期文档：

```text
docs/
  implementation-plan.md          # 初始实施计划
  alphagbm-skills-adaptation.md   # 方法论/技能迁移说明
  output-and-docs.md              # 本文件
```

适合放入 `docs/` 的内容：

```text
- 研究方法论
- 长期有效的策略说明
- 脚本地图
- 输出字段解释
- 上线/风控规则
- 复现实验步骤
```

不适合放入 `docs/` 的内容：

```text
- 每次运行生成的大结果
- SQLite/CSV/Parquet 数据
- 含敏感路径或凭据的日志
- 高频变化的临时调参记录
```

## `output/`

`output/` 是本地结果目录，默认被 `.gitignore` 排除。

典型输出包括：

```text
output/old-radar-effectiveness-substantiation-latest.md
output/hotcoin-execution-proxy-search-latest.md
output/hotcoin-execution-antioverfit-latest.md
output/hotcoin-execution-recent24h-h24sl60-latest.md
output/hotcoin-execution-recent3-h24sl60-latest.md
output/old-execution-alpha-full-window-latest.md
output/old-execution-alpha-primary-robustness-latest.md
```

这些 artifact 的作用：

| 类型 | 作用 |
|---|---|
| `*-latest.md` | 给人看的最新结论 |
| `*-latest.json` | 给脚本/后续分析读的结构化结果 |
| 带时间戳文件 | 保留某次运行快照，便于回溯 |

## 是否上传 `output/`？

默认不上传。

原因：

```text
1. 可能包含本地数据路径；
2. 文件会频繁变化；
3. SQLite/CSV/大结果会污染 repo；
4. 策略结论应该沉淀到 README/docs，而不是依赖本地 output。
```

如果某个结果非常关键，需要长期保留，推荐做法：

```text
1. 从 output/*.md 中摘取核心数字；
2. 写入 README.md 或 docs/*.md；
3. 不直接提交完整 output 文件。
```

## 当前推荐 artifact 阅读顺序

```text
1. Discovery / 热币雷达有效性
   output/old-radar-effectiveness-substantiation-latest.md

2. Execution / 策略执行几何
   output/hotcoin-execution-proxy-search-latest.md

3. Anti-overfit / 反过拟合
   output/hotcoin-execution-antioverfit-latest.md

4. Recent 24h / 最新完整日内窗口
   output/hotcoin-execution-recent24h-h24sl60-latest.md

5. Recent 3 days / 最近3个完整 BJT 日
   output/hotcoin-execution-recent3-h24sl60-latest.md
```

## 代码目录与 output 的关系

```text
scripts/discovery/   -> output/*discovery*, output/old-radar-effectiveness-*
scripts/execution/   -> output/*execution*, output/hotcoin-execution-proxy-*
scripts/validation/  -> output/*validate*, output/*antioverfit*, output/*recent24h*
scripts/ops/         -> output/*audit*, output/*shadow*, output/*signal-control*
```
