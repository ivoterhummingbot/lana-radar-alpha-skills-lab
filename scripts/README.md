# Scripts 分类说明

本目录只保留当前仍用于 Lana 做多热币雷达研究/验证/线上 shadow 的活跃脚本。

历史分支已移动到：

```text
backup/retired-scripts-2026-06/
```

主目录结构：

```text
scripts/
  discovery/   # 热币雷达 / 发现层 / selector 有效性
  execution/   # 策略执行层：入场、退出、持有、止损、搜索
  validation/  # 反过拟合、recent24h/recent48h/recent3、robustness 验证
  ops/         # 输入审计、shadow tracking、score/regime/signal 审计
```

> `output/` 是本地研究结果目录，默认被 `.gitignore` 排除；GitHub 只保留代码和文档，不上传 SQLite/CSV/结果大文件。

## 当前主线

```text
old radar hotcoin selector
+ market_confirmation_score top10
+ same-symbol cooldown 60m
+ 24h hold
+ -6% hard stop
+ all-taker 8bp
```

## 1. discovery/：热币雷达 / 发现层

这些脚本回答：

```text
老雷达是否能从热币/动量池里选出后续更强的币？
market_confirmation_score / momentum score 是否有 selector 效果？
发现信号在哪个 horizon 有效？
```

| 脚本 | 当前状态 | 用途 | 常见输出 |
|---|---|---|---|
| `substantiate_old_radar_effectiveness.py` | 主线 | 当前最重要的热币 discovery/selector 佐证；验证 old_core_market_top20、old_momentum 等是否打过 same-ts random | `output/old-radar-effectiveness-substantiation-latest.md` |
| `run_old_radar_alpha_search.py` | 保留 | 老雷达 delayed alpha / selector 组合搜索；较重 | `output/old-radar-delayed-alpha-search.md` |
| `run_old_radar_fixed_shadow_replay.py` | 保留 | 老雷达固定 shadow 24h OHLC replay | `output/old-radar-fixed-shadow-24h-ohlc-replay.md` |
| `validate_discovery_horizon_15m_30m_1h_4h_clean.py` | 保留 | 验证 discovery horizon：15m/30m/1h/4h 哪个有效 | `output/discovery-horizon-*.md` |
| `validate_full_data_first_layer_discovery_fast_clean.py` | 依赖保留 | 历史 first-layer 快速验证；仍被少量旧脚本 import，第二轮重构后可考虑归档 | `output/full-data-first-layer-discovery-fast-clean-*.md` |
| `run_radar_effectiveness.py` | 保留 | 雷达有效性通用验证入口 | `output/*radar-effectiveness*.md` |

核心源码：

```text
src/radar_alpha_skills_lab/old_radar_alpha.py
src/radar_alpha_skills_lab/old_radar_replay.py
```

## 2. execution/：策略执行层

这些脚本回答：

```text
已经发现热币之后，应该怎么交易？
短线 PB/trailing 还是 24h continuation？
止损、止盈、持有时间、cooldown、topN 怎么设？
```

| 脚本 | 当前状态 | 用途 | 常见输出 |
|---|---|---|---|
| `search_hotcoin_execution_proxy.py` | 主线 | 热币执行几何搜索；当前 24h continuation + -6% stop 结论来源 | `output/hotcoin-execution-proxy-search-latest.md` |
| `search_hotcoin_execution_scheme.py` | 待重构 | 热币执行方案 OHLC/路径搜索尝试；较重，未来可重写 | `output/hotcoin-execution-scheme-search*.md` |
| `search_old_execution_alpha_second_stage.py` | 依赖保留 | 老雷达执行 alpha 第二阶段搜索；仍被部分验证脚本依赖 | `output/old-execution-alpha-second-stage-latest.md` |
| `search_execution_alpha_focused_v2.py` | 依赖保留 | focused v2 execution 搜索核心；当前 `substantiate_old_radar_effectiveness.py` 仍 import | `output/*execution-alpha*.md` |
| `search_execution_alpha_narrow.py` | 依赖保留 | 窄参数搜索基础模块；被 focused v2 等脚本依赖 | `output/*narrow*.md` |

## 3. validation/：验证、反过拟合、robustness

这些脚本回答：

```text
策略是不是过拟合？
最近3天/最近24h 是否继续有效？
剔除 top symbols 后是否仍然成立？
全窗口、日切、same-ts random95 是否通过？
```

| 脚本 | 当前状态 | 用途 | 常见输出 |
|---|---|---|---|
| `validate_hotcoin_execution_antioverfit.py` | 主线 | 当前主规则反过拟合验证：全窗口/剔近期/邻近池/remove-top5 | `output/hotcoin-execution-antioverfit-latest.md` |
| `validate_hotcoin_execution_recent24h.py` | 主线 | 最近可完成 24h outcome 验证；Sharpe、最高收益、stop 率 | `output/hotcoin-execution-recent24h-h24sl60-latest.md` |
| `validate_hotcoin_execution_recent48h.py` | 主线 | 最近可完成 48h 信号窗口验证；每笔仍用 24h outcome，检查两日连续性/集中度/stop 率 | `output/hotcoin-execution-recent48h-h24sl60-latest.md` |
| `validate_hotcoin_execution_recent3.py` | 主线 | 最近3个完整 BJT 日验证 | `output/hotcoin-execution-recent3-h24sl60-latest.md` |
| `validate_hotcoin_execution_targeted.py` | 待重构 | targeted OHLC 验证尝试；较重 | `output/*targeted*.md` |
| `validate_old_execution_alpha_full_window.py` | 反证保留 | 证明旧 PB/trailing 执行层全窗口不稳定 | `output/old-execution-alpha-full-window-latest.md` |
| `validate_old_execution_alpha_daily.py` | 反证保留 | 老雷达执行层逐日验证 | `output/old-execution-alpha-daily-validate-latest.md` |
| `validate_old_execution_alpha_primary_robustness.py` | 反证保留 | primary candidate robustness | `output/old-execution-alpha-primary-robustness-latest.md` |
| `validate_c_oldcore_cd60_daily.py` | 依赖保留 | oldcore cd60 逐日验证；被 dynamic/旧验证链依赖 | `output/*oldcore-cd60*.md` |
| `validate_execution_alpha_focused_v2_top.py` | 依赖保留 | focused v2 top 候选验证 | `output/*focused-v2-top*.md` |
| `compare_four_shadows_all_snapshots.py` | 保留 | 四条 shadow 线对比 | `output/*four-shadows*.md` |
| `spike_prev5m_alpha_search.py` | 依赖保留 | prev5m 基础搜索；第二轮重构后可归档 | `output/*prev5m*.md` |

## 4. ops/：线上/运维/审计

这些脚本回答：

```text
输入数据库是否可读？
shadow tracking 如何运行？
score/regime/signal/control 是否符合边界？
```

| 脚本 | 当前状态 | 用途 | 常见输出 |
|---|---|---|---|
| `audit_readonly_inputs.py` | 主线 | 只读输入数据库审计 | `output/readonly-input-audit.md` |
| `run_c_cd60_shadow_tracking.py` | 主线 | C/CD60 shadow tracking | `output/*shadow*.md` |
| `run_score_regime_audit.py` | 保留 | score/regime 审计 | `output/*score-regime*.md` |
| `run_signal_control_audit.py` | 保留 | signal/control 审计 | `output/*signal-control*.md` |

## 推荐运行顺序

```text
1. discovery/substantiate_old_radar_effectiveness.py
   先确认热币雷达 selector 是否仍有效。

2. execution/search_hotcoin_execution_proxy.py
   再确认执行几何：24h continuation / -6% stop 是否优于短线。

3. validation/validate_hotcoin_execution_antioverfit.py
   排除纯最近窗口过拟合。

4. validation/validate_hotcoin_execution_recent24h.py
   检查最新完整 24h 表现、Sharpe、最高收益、stop rate。

5. validation/validate_hotcoin_execution_recent48h.py
   检查最近 48h 信号窗口下两日连续性、集中度、stop rate。

6. ops/run_c_cd60_shadow_tracking.py
   进入 live shadow / paper tracking。
```

## 退役脚本

第一轮清理已把以下分支移动到 `backup/retired-scripts-2026-06/`：

```text
- first-layer / new-radar historical scripts
- PB/trailing/45m short-execution experiments
- dynamic-exit experiments
- prev5m spike branch validations
- stale recent/backtest entry scripts
```

如需恢复，先从 backup 移回对应目录，再运行：

```bash
python3 -m compileall -q scripts src tests
python3 -m unittest discover -s tests -v
```
