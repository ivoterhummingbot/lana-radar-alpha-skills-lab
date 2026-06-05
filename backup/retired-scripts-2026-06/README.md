# Retired Scripts Backup — 2026-06

这个目录存放从主 `scripts/` 目录清理出来的历史研究脚本。

目的：

```text
1. 让主线目录只保留当前做多热币雷达相关脚本；
2. 保留历史探索代码，方便必要时回看；
3. 避免 first-layer/new-radar、prev5m、dynamic-exit、PB/trailing 分支继续干扰当前主线。
```

## 状态

这些脚本是 **retired / not active**：

```text
不再作为当前热币 24h continuation 主线的一部分；
不保证移动后仍可直接运行；
如需恢复，应先移回 scripts/ 对应分类目录并重新跑 compile/test/smoke。
```

## 为什么归档

当前主线已经收敛为：

```text
old radar hotcoin selector
+ market_confirmation_score top10
+ same-symbol cooldown 60m
+ 24h hold
+ -6% hard stop
```

被归档的分支主要包括：

```text
- first-layer / new-radar 历史验证；
- PB/trailing/45m 短执行搜索；
- dynamic exit 探索；
- prev5m spike 分支；
- 过期的 recent/backtest 入口。
```

## 归档清单

### discovery/

```text
generate_alpha_discovery_blueprint.py
validate_full_data_first_layer_discovery_clean.py
validate_tonight_first_layer_discovery.py
```

### execution/

```text
backtest_best_execution_recent24h.py
deep_exit_strategy_diagnostics.py
run_radar_execution_node_search.py
search_execution_alpha_fast_pilot.py
search_execution_alpha_focused_v2_fast.py
validate_dynamic_exit_alpha.py
verify_dynamic_exit_candidate.py
```

### validation/

```text
spike_old_bc_prev5m_ohlc_revalidate.py
spike_prev5m_shortlist_revalidate.py
validate_a_night_rel5_fresh_today.py
validate_prev5m_alpha_overfit.py
validate_prev5m_other_fresh_candidates.py
```

## 恢复方式

例如恢复某个脚本：

```bash
mv backup/retired-scripts-2026-06/execution/<script>.py scripts/execution/
python3 -m compileall -q scripts src tests
python3 -m unittest discover -s tests -v
```
