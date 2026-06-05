# Scripts 分类说明

本目录按研究流程拆成四类，避免把 discovery、execution、validation 和线上/运维脚本混在一起。

```text
scripts/
  discovery/   # 热币雷达 / 发现层 / selector 有效性
  execution/   # 策略执行层：入场、退出、持有、止损、搜索
  validation/  # 反过拟合、recent24h/recent3、robustness、spike 验证
  ops/         # 输入审计、shadow tracking、score/regime/signal 审计
```

> 说明：`output/` 是本地研究结果目录，默认被 `.gitignore` 排除；GitHub 只保留代码和文档，不上传 SQLite/CSV/结果大文件。

## 1. discovery/：热币雷达 / 发现层

这些脚本回答：

```text
老雷达是否能从热币/动量池里选出后续更强的币？
market_confirmation_score / momentum score 是否有 selector 效果？
发现信号在哪个 horizon 有效？
```

核心脚本：

| 脚本 | 用途 | 常见输出 |
|---|---|---|
| `substantiate_old_radar_effectiveness.py` | 当前最重要的热币 discovery/selector 佐证；验证 old_core_market_top20、old_momentum 等是否打过 same-ts random | `output/old-radar-effectiveness-substantiation-latest.md` |
| `run_old_radar_alpha_search.py` | 老雷达 delayed alpha / selector 组合搜索 | `output/old-radar-delayed-alpha-search.md` |
| `run_old_radar_fixed_shadow_replay.py` | 老雷达固定 shadow 24h OHLC replay | `output/old-radar-fixed-shadow-24h-ohlc-replay.md` |
| `validate_discovery_horizon_15m_30m_1h_4h_clean.py` | 验证 discovery horizon：15m/30m/1h/4h 哪个有效 | `output/discovery-horizon-*.md` |
| `validate_full_data_first_layer_discovery_fast_clean.py` | 全数据 first-layer discovery 快速验证；偏历史/对照 | `output/full-data-first-layer-discovery-fast-clean-*.md` |
| `validate_full_data_first_layer_discovery_clean.py` | 全数据 first-layer discovery 完整版；更慢 | `output/full-data-first-layer-discovery-*.md` |
| `validate_tonight_first_layer_discovery.py` | 最新/今晚 discovery smoke | `output/tonight-first-layer-discovery-*.md` |
| `generate_alpha_discovery_blueprint.py` | 生成 discovery 研究蓝图 | `output/alpha-discovery-blueprint.md` |
| `run_radar_effectiveness.py` | 雷达有效性通用验证入口 | `output/*radar-effectiveness*.md` |

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

核心脚本：

| 脚本 | 用途 | 常见输出 |
|---|---|---|
| `search_hotcoin_execution_proxy.py` | 热币执行几何搜索；当前 24h continuation + -6% stop 结论来源 | `output/hotcoin-execution-proxy-search-latest.md` |
| `search_hotcoin_execution_scheme.py` | 热币执行方案 OHLC/路径搜索尝试；较重 | `output/hotcoin-execution-scheme-search*.md` |
| `search_old_execution_alpha_second_stage.py` | 老雷达执行 alpha 第二阶段搜索 | `output/old-execution-alpha-second-stage-latest.md` |
| `search_execution_alpha_focused_v2.py` | focused v2 execution 搜索核心，被多个验证脚本复用 | `output/*execution-alpha*.md` |
| `search_execution_alpha_focused_v2_fast.py` | focused v2 快速版 | `output/*focused-v2-fast*.md` |
| `search_execution_alpha_fast_pilot.py` | 快速 pilot 搜索 | `output/*fast-pilot*.md` |
| `search_execution_alpha_narrow.py` | 窄参数搜索基础模块 | `output/*narrow*.md` |
| `backtest_best_execution_recent24h.py` | 最近24h最佳执行回测 | `output/*recent24h*.md` |
| `run_radar_execution_node_search.py` | execution node search 入口 | `output/*execution-node*.md` |
| `deep_exit_strategy_diagnostics.py` | 深入诊断 exit strategy | `output/*exit-diagnostics*.md` |
| `validate_dynamic_exit_alpha.py` | dynamic exit alpha 验证 | `output/*dynamic-exit*.md` |
| `verify_dynamic_exit_candidate.py` | dynamic exit 候选复核 | `output/*dynamic-exit*.md` |

当前主策略规则：

```text
market_confirmation_score top10
+ same-symbol cooldown 60m
+ 24h hold
+ -6% hard stop
+ all-taker 8bp
```

## 3. validation/：验证、反过拟合、robustness

这些脚本回答：

```text
策略是不是过拟合？
最近3天/最近24h 是否继续有效？
剔除 top symbols 后是否仍然成立？
全窗口、日切、same-ts random95 是否通过？
```

核心脚本：

| 脚本 | 用途 | 常见输出 |
|---|---|---|
| `validate_hotcoin_execution_antioverfit.py` | 当前主规则反过拟合验证：全窗口/剔近期/邻近池/remove-top5 | `output/hotcoin-execution-antioverfit-latest.md` |
| `validate_hotcoin_execution_recent24h.py` | 最近可完成 24h outcome 验证；Sharpe、最高收益、stop 率 | `output/hotcoin-execution-recent24h-h24sl60-latest.md` |
| `validate_hotcoin_execution_recent3.py` | 最近3个完整 BJT 日验证 | `output/hotcoin-execution-recent3-h24sl60-latest.md` |
| `validate_hotcoin_execution_targeted.py` | targeted OHLC 验证尝试；较重 | `output/*targeted*.md` |
| `validate_old_execution_alpha_full_window.py` | 老雷达 PB/trailing 执行层全窗口验证 | `output/old-execution-alpha-full-window-latest.md` |
| `validate_old_execution_alpha_daily.py` | 老雷达执行层逐日验证 | `output/old-execution-alpha-daily-validate-latest.md` |
| `validate_old_execution_alpha_primary_robustness.py` | primary candidate robustness | `output/old-execution-alpha-primary-robustness-latest.md` |
| `validate_execution_alpha_focused_v2_top.py` | focused v2 top 候选验证 | `output/*focused-v2-top*.md` |
| `validate_c_oldcore_cd60_daily.py` | oldcore cd60 逐日验证 | `output/*oldcore-cd60*.md` |
| `validate_a_night_rel5_fresh_today.py` | 夜盘 fresh 今日 smoke | `output/*night-rel5*.md` |
| `validate_prev5m_alpha_overfit.py` | prev5m alpha 过拟合检查 | `output/*prev5m*.md` |
| `validate_prev5m_other_fresh_candidates.py` | prev5m 其他候选新鲜验证 | `output/*prev5m*.md` |
| `spike_prev5m_alpha_search.py` | prev5m spike 搜索 | `output/*prev5m*.md` |
| `spike_prev5m_shortlist_revalidate.py` | prev5m shortlist 复核 | `output/*prev5m*.md` |
| `spike_old_bc_prev5m_ohlc_revalidate.py` | old B/C prev5m OHLC 复核 | `output/*prev5m*.md` |
| `compare_four_shadows_all_snapshots.py` | 四条 shadow 线对比 | `output/*four-shadows*.md` |

## 4. ops/：线上/运维/审计

这些脚本回答：

```text
输入数据库是否可读？
shadow tracking 如何运行？
score/regime/signal/control 是否符合边界？
```

| 脚本 | 用途 | 常见输出 |
|---|---|---|
| `audit_readonly_inputs.py` | 只读输入数据库审计 | `output/readonly-input-audit.md` |
| `run_c_cd60_shadow_tracking.py` | C/CD60 shadow tracking | `output/*shadow*.md` |
| `run_score_regime_audit.py` | score/regime 审计 | `output/*score-regime*.md` |
| `run_signal_control_audit.py` | signal/control 审计 | `output/*signal-control*.md` |

## 推荐使用顺序

```text
1. discovery/substantiate_old_radar_effectiveness.py
   先确认热币雷达 selector 是否仍有效。

2. execution/search_hotcoin_execution_proxy.py
   再确认执行几何：24h continuation / -6% stop 是否优于短线。

3. validation/validate_hotcoin_execution_antioverfit.py
   排除纯最近窗口过拟合。

4. validation/validate_hotcoin_execution_recent24h.py
   检查最新完整 24h 表现、Sharpe、最高收益、stop rate。

5. ops/run_c_cd60_shadow_tracking.py
   进入 live shadow / paper tracking。
```
