# AlphaGBM Skills → Lana Radar Alpha 迁移分析

## 结论

AlphaGBM/skills 对当前 crypto radar **可以用，但不是直接调用 API 用**。

原因：

- AlphaGBM skills 的数据覆盖是 US/HK/CN 股票、ETF、期权、商品期权。
- 当前 Lana radar 的对象是 crypto hotcoin discovery/execution。
- `ALPHAGBM_API_KEY` 当前未配置；即使配置，API 也不直接覆盖我们需要的 Binance/MEXC/HTX crypto radar path。

真正可迁移的是 AlphaGBM 的研究结构：

```text
1. signal vs control
2. fixed composite scoring
3. regime-aware filtering
4. exit-family lab
5. watchlist/alert/health-check discipline
```

这套结构适合重新找 radar alpha，尤其适合避免“同批样本上继续调阈值”的过拟合。

---

## 当前不可动的既有 shadow

本项目不会修改现有项目，不改变两个已保留 shadow lane：

```text
shadow_1_primary:
  new_radar + not_momentum_prev5m + managed_1h + score-ranked cap5

shadow_2_safe:
  new_radar + not_momentum_prev5m + managed_1h + score-ranked cap20
```

它们继续由原项目负责 fresh-forward / shadow 追踪。

新项目只负责 **研究新 alpha 或审计框架**，不能覆盖原 shadow 结论。

---

## Skill 逐项可用性

### A. 高价值，可直接迁移方法论

#### 1. alphagbm-bps-backtest

原逻辑：

```text
同一策略参数下：with_signal vs no_signal control
```

迁移为：

```text
signal: frozen radar rule
control_1: same timestamp matched-N random
control_2: all new_radar at same timestamps
control_3: top unified_discovery_score at same timestamps
control_4: no-prev5m ablation
```

用途：

- 判断 alpha 来自规则本身，还是来自时间戳/basket beta。
- 避免只看 signal ROI。
- 对当前 `not_momentum_prev5m managed_1h` 做更强审计。

优先级：最高。

#### 2. alphagbm-market-sentiment / alphagbm-vix-status / alphagbm-marks-cycle

原逻辑：

```text
市场恐惧、breadth、cycle/regime 决定交易姿态。
```

迁移为：

```text
btc_regime_state
btc_relative_gate_permission
alt_breadth_1h / 4h / 24h
hot_count
BJT session
market-wide same-timestamp return dispersion
```

用途：

- 找出新雷达 execution edge 是否只在特定 regime 成立。
- 区分“信号 alpha”与“市场状态 beta”。
- 形成 gating：core night / btc allow / breadth healthy 等。

优先级：高。

#### 3. alphagbm-take-profit

原逻辑：

```text
同一 entry 比较 15 种 exit，并用 rollercoaster rate 判断是否适合 hold。
```

迁移为：

```text
同一 radar entry 比较：
hold_15m / hold_30m / hold_60m / hold_120m
managed_15m / managed_1h
tp1_tail / tp2_tail
hard_sl / trail / partial_take
```

新增 crypto 指标：

```text
edge_evaporation_rate = MFE 达标后最终回撤/转负的比例
same_bar_stop_first = 同一根 OHLC 同时触达 TP/SL 时保守记 SL
```

用途：

- 验证 `managed_1h` 是否真的最优，而不是历史偶然。
- 找出不同 signal family 对应不同 exit。

优先级：高。

#### 4. alphagbm-fear-score

原逻辑：

```text
固定权重多因子 panic composite，组件透明，阈值后验审计。
```

迁移为 crypto attention composite：

```text
positive:
  community_heat_score
  source_diversity
  freshness_score
  attention_spread_score
  prev5m_confirmation_score
  volume_ratio_1h
  symbol_rel_5m_vs_btc

negative:
  warning_score
  fomo_risk_score
  prev5m_upper_wick_ratio
  too_close_to_24h_high
  btc_breakdown_30m/1h
```

用途：

- 重新找 discovery/execution entry score。
- 每个 entry 输出组件贡献，避免黑箱。

优先级：中高，但必须晚于 signal/control；否则容易变成新一轮调参。

### B. 中等价值，适合研究管理

#### alphagbm-watchlist / alphagbm-alert

迁移为 shadow board：

```text
每条 shadow lane 维护：
  rule
  status
  last validation window
  fee model
  venue assumptions
  kill conditions
  fresh-forward metrics
```

#### alphagbm-investment-thesis / alphagbm-health-check

迁移为 alpha thesis registry：

```text
每条 alpha 必须写明：
  为什么存在
  什么时候失效
  上次审计结果
  是否 stale
  是否 drift
  是否过拟合风险上升
```

用途：

- 防止历史结论丢失。
- 防止旧 alpha 在新数据里失效后仍被引用。

### C. 低价值，不建议用于当前问题

```text
options-score
greeks
vol-surface
vol-smile
iv-rank
earnings-crush
options-strategy
pnl-simulator
buffett/duan/tepper/marks investor style
polymarket
```

原因：

- 主要面向股票/期权。
- 不直接提供 crypto 1m/15m path。
- 容易把不相关的期权术语引入当前 radar 问题。

---

## 推荐重找 alpha 流程

### Phase 1: 不改现有系统，只做 read-only audit

目标：确认数据源、字段、窗口可用。

输出：

```text
readonly-input-audit
```

验证内容：

```text
maker_attn_symbol_scores 字段
maker_attn_market_snapshots 字段
lana_community_scores 字段
community_forward_outcomes 字段
market_snapshots 字段
```

### Phase 2: BPS-style signal/control

目标：用同窗口同成本证明信号相对 control 有贡献。

候选 signal：

```text
new_radar_not_momentum_prev5m
new_radar_score_top_decile
new_radar_low_fomo_high_freshness
community_heat_top + market/momentum lag
```

control：

```text
same_timestamp_random
same_timestamp_all_watch
same_timestamp_top_unified_score
same_timestamp_market_beta
```

指标：

```text
avg/signal
win
signal comp
portfolio comp cap5/cap20
MDD
random p-value
top-symbol removal
day-wise / BJT-session-wise
```

晋级条件：

```text
signal > random p95
cap 后为正
top5 removal 后不崩
至少 2 个 BJT session 或 2 个自然日仍可解释
```

### Phase 3: Regime gate

目标：找出 edge 是否只在某些 regime 下成立。

切分：

```text
btc_relative_gate_permission
btc_regime_state
alt_breadth quartile
hot_count quartile
BJT session
```

输出：

```text
signal × regime matrix
```

晋级条件：

```text
某个 gate 能降低回撤，同时不只靠一个 symbol/day。
```

### Phase 4: Exit lab

目标：验证 entry 与 exit 的配对。

同一 entry 跑 exit family：

```text
fixed hold: 15m, 30m, 60m, 120m
managed: 15m, 1h
tp/sl family
partial take family
```

输出：

```text
exit ranking
edge_evaporation_rate
same-bar conservative replay
fee/slip sensitivity
```

晋级条件：

```text
最佳 exit 在外部窗口仍强于相邻 exits；不是单一 TP/SL 曲线拟合。
```

### Phase 5: Fixed composite search

目标：在上述结果稳定后，再做固定权重 composite，不先暴力调参。

流程：

```text
1. 固定组件方向
2. 固定 winsorize / rank transform
3. 固定 3-5 套权重
4. 只看 decile / tercile，不调细阈值
5. external/fresh 再确认
```

---

## 最小实现边界

本新项目只做：

```text
read-only inputs -> analysis reports -> candidate alpha blueprints
```

不做：

```text
不写原项目 DB
不改原脚本
不启动自动交易
不替换现有 shadow lane
不写生产配置
```

---

## 推荐下一步实现顺序

1. `data_contract.py`：只读数据合约检查。
2. `signal_control.py`：signal/control 数据集构造与评分。
3. `regime_gate.py`：regime matrix。
4. `exit_lab.py`：exit family ranking。
5. `composite_search.py`：固定 composite 试验。
6. `shadow_registry.py`：记录候选 alpha 的 thesis/kill-condition。

每一步都要输出 markdown + json，并且报告中明确：

```text
DISCOVERY vs EXECUTION
GROSS vs NET
SIGNAL SUM vs PORTFOLIO COMP
TRAIN vs EXTERNAL/FRESH
```
