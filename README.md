# Lana 做多雷达 Alpha Lab

这是一个只读研究仓库，用于验证 Lana 热门山寨币 / 做多雷达的发现能力、执行层可行性和线上 shadow 规则。

本仓库的核心结论：

```text
老雷达不是独立“发现新币”的万能 discovery 引擎；
但它可以作为热门山寨币 continuation selector。

当 discovery 已经有效时，执行层不应做 45m 短打，
更适合 market top10 + 60m cooldown + 24h continuation + -6% hard stop。
```

> 重要：本项目默认只读读取外部 Lana 数据库与历史输出，不修改生产雷达项目，不包含交易所 API key，不包含本地 `output/` 结果文件和 SQLite 数据库。

---

## 目录

- [项目边界](#项目边界)
- [做多雷达原理](#做多雷达原理)
- [策略族介绍](#策略族介绍)
- [可行性分析](#可行性分析)
- [测试周期与主要结果](#测试周期与主要结果)
- [效果 Demo](#效果-demo)
- [如何复现实验](#如何复现实验)
- [上线建议](#上线建议)
- [风险与限制](#风险与限制)

---

## 项目边界

- Source project：`lana-community-hotcoin-analyzer`
- Research lab：`lana-radar-alpha-skills-lab`
- 本仓库只读使用 source project 的 SQLite / 输出文件。
- 本仓库自己的结果写入 `output/`，但 `output/` 默认不提交 git。
- 不提交：数据库、CSV、密钥、缓存、临时 artifact。

---

## 做多雷达原理

### 1. Discovery 与 Execution 分层

雷达被拆成两层：

```text
Discovery / Selector：
  从大量山寨币里识别当前正在被市场确认的热门币。

Execution：
  在已确认热门币后，决定如何进场、持有、止损和退出。
```

早期验证发现：

```text
老雷达的强点不在“提前发现所有新热点”，
而在“热门币池内的 market score 排序 + 24h continuation”。
```

因此后续不再把老雷达包装成独立 discovery engine，而是作为：

```text
hotcoin continuation selector
```

### 2. 核心信号

主要使用老雷达输出中的：

```text
market_confirmation_score
momentum_confirmation_score
session
hour_bjt
decision_status
recommended_action
return_24h / mfe_24h / mae_24h
```

核心选择方式：

```text
同一时间戳内按 market_confirmation_score 排序；
只取 top10% / top20% / top33%；
同币 60 分钟 cooldown，避免重复追单；
用 same-ts random95 做对照。
```

### 3. 为什么是 24h continuation

短线执行层曾测试过：

```text
PB10_w25 + TR08_T05_45
```

即 1% 回踩、45m trailing。这条在最近上涨 regime 有效，但全窗口失败：

```text
全窗口 avg +0.15% vs random95 +0.20%
strict day pass 3/16
initial_stop 32.4%
```

说明 45m 短打不匹配老雷达的真实优势。

重新按 4h / 24h continuation 搜索后，发现最强几何是：

```text
market top10
+ same-symbol cooldown 60m
+ 24h hold
+ -6% hard stop
```

---

## 策略族介绍

所有当前主要策略共享同一个执行规则：

```text
market_confirmation_score top10
same-symbol cooldown 60m
24h hold
-6% hard stop
all-taker cost = 8bp
```

不同点只在候选池入口。

### 1. `core_mkt10_cd60 + H24_sl60`

候选池：

```text
BJT 20:00 - 08:00 核心交易窗口
market_confirmation_score top10%
同币 60m cooldown
```

特点：

- 覆盖较广。
- 是当前主观察线。
- 全窗口和反过拟合验证最强。
- 最近 24h 收益强，但 stop 和集中度偏高。

适合：

```text
live shadow 主线 / paper tracking
```

### 2. `night_mkt10_cd60 + H24_sl60`

候选池：

```text
BJT 20:00 - 04:00 夜盘窗口
market_confirmation_score top10%
同币 60m cooldown
```

特点：

- 更聚焦夜盘爆发。
- 收益弹性强。
- 最近 24h 依赖 OPN，remove-top 后偏弱。

适合：

```text
夜盘 sibling / 对照线
```

### 3. `day_high_mkt10_cd60 + H24_sl60`

候选池：

```text
session = day_high_threshold
market_confirmation_score top10%
同币 60m cooldown
```

特点：

- 信号少，但质量更干净。
- 最近 24h Sharpe、win rate、stop rate 最好。
- 更适合 canary 优先级。

适合：

```text
低风险 shadow / 极小仓 canary 候选
```

### 4. `all_mkt10_cd60 + H24_sl60`

候选池：

```text
全部老雷达信号
market_confirmation_score top10%
同币 60m cooldown
```

特点：

- 覆盖最广。
- 作为 benchmark 很强。
- 实盘可能过度拥挤、相关性高。

适合：

```text
研究上限 / benchmark，不建议直接全量实盘
```

---

## 可行性分析

### Discovery 有效性

老雷达全窗口验证显示，热门币 24h continuation 明显存在：

```text
old_core_market_top20
24h ret_avg +18.33%
24h MFE_avg +23.49%
positive_days 15/15
```

这说明：

```text
老雷达可以识别已经进入热度扩散/延续状态的山寨币。
```

### Execution 几何

短线 45m 执行不稳定，但 24h continuation 执行更匹配信号 horizon。

当前主执行规则：

```text
H24_sl60 = 24h hold + -6% hard stop
```

净亏损显示约 `-6.08%`，因为扣除了 all-taker 8bp 成本。

### 反过拟合检查

主线 `core_mkt10_cd60 + H24_sl60` 通过：

- full window
- 剔除最近 3 天后的 prior window
- recent 3 days
- remove top5 symbols
- day-wise random95
- neighboring pools

代表它不是只靠最近几天拟合出来的策略。

---

## 测试周期与主要结果

### 数据窗口

当前主要研究窗口：

```text
2026-05-20 ~ 2026-06-04
```

最近 24h 完整 outcome 窗口：

```text
UTC: 2026-06-03 01:46:50 -> 2026-06-04 01:46:50
BJT: 2026-06-03 09:46:50 -> 2026-06-04 09:46:50
```

### 全窗口 / 反过拟合结果

`core_mkt10_cd60 + H24_sl60`：

```text
full:
  n = 288
  avg = +18.19% vs random95 +9.57%
  edge = +8.62%
  remove_top5_avg = +16.98%
  positive_days = 16/16
  strict_days = 15/16
  stop = 16.7%

prior_ex_recent3:
  n = 232
  avg = +16.49% vs random95 +9.24%
  edge = +7.25%
  remove_top5_avg = +15.35%
  positive_days = 13/13
  stop = 15.1%

recent3:
  n = 56
  avg = +25.24% vs random95 +13.84%
  edge = +11.40%
  remove_top5_avg = +13.84%
  positive_days = 3/3
  stop = 23.2%
```

邻近池也一致支持：

```text
night_mkt10_cd60 full edge +7.90%, positive_days 16/16
all_mkt10_cd60 full edge +8.39%, positive_days 16/16
watch_hot_mkt10_cd60 full edge +6.63%, positive_days 16/16
core_mkt20_cd60 full edge +6.31%, positive_days 16/16
```

---

## 效果 Demo

### 最近 24h：`core_mkt10_cd60 + H24_sl60`

```text
n = 25 / 390
avg = +25.40% vs random95 +14.39%
edge = +11.01%
sum = +634.92% vs random95 +359.78%
Sharpe-like = 3.81 vs random95 3.54
win = 60.0%
hard_stop = 40.0%
max_trade = OPN +115.98%
min_trade = VVV -6.08%
remove_top5_avg = -1.58%
```

解释：

```text
收益仍然有效，但最近 24h core 集中度较高；
剔除 top5 后略转负，因此不宜直接放大仓位。
```

### 最近 24h：`day_high_mkt10_cd60 + H24_sl60`

```text
n = 14 / 203
avg = +23.02% vs random95 +16.39%
edge = +6.63%
sum = +322.25% vs random95 +229.40%
Sharpe-like = 5.38 vs random95 3.95
win = 85.7%
hard_stop = 14.3%
max_trade = APR +50.29%
min_trade = US -6.08%
remove_top5_avg = +7.23%
```

解释：

```text
day_high 不是收益最高，
但最近 24h 更干净：Sharpe 高、win 高、stop 低、remove-top 后仍为正。
```

### 最高收益案例

`core/night/all` 最近 24h 最大单笔：

```text
OPN +115.98%
return24 = +116.06%
MFE24 = +125.54%
MAE24 = -3.74%
exit = time_exit
BJT = 2026-06-04 03:34:44
```

`day_high` 最近 24h 最大单笔：

```text
APR +50.29%
return24 = +50.37%
MFE24 = +52.31%
MAE24 = -1.64%
exit = time_exit
BJT = 2026-06-03 11:11:54
```

---

## 如何复现实验

### 环境

```bash
python3 -m unittest discover -s tests -v
```

### 主要脚本

```bash
# 老雷达有效性佐证
python3 scripts/substantiate_old_radar_effectiveness.py

# 45m 执行层全窗口复核
python3 scripts/validate_old_execution_alpha_full_window.py

# 24h 热币执行几何搜索，proxy 方法
python3 scripts/search_hotcoin_execution_proxy.py

# 最近 3 天验证
python3 scripts/validate_hotcoin_execution_recent3.py

# 反过拟合验证
python3 scripts/validate_hotcoin_execution_antioverfit.py

# 最近 24h 验证
python3 scripts/validate_hotcoin_execution_recent24h.py
```

### 主要输出

输出默认在 `output/`，例如：

```text
output/hotcoin-execution-proxy-search-latest.md
output/hotcoin-execution-recent3-h24sl60-latest.md
output/hotcoin-execution-antioverfit-latest.md
output/hotcoin-execution-recent24h-h24sl60-latest.md
```

`output/` 不提交 git，因为它可能包含大量本地研究 artifact。

---

## 上线建议

当前推荐级别：

```text
live shadow / paper: YES
极小仓 canary: 可谨慎考虑
full production: NO
```

推荐 shadow 分层：

```text
主观察：
  core_mkt10_cd60 + H24_sl60

低风险优先：
  day_high_mkt10_cd60 + H24_sl60

夜盘对照：
  night_mkt10_cd60 + H24_sl60
```

如果做 canary，优先：

```text
day_high_mkt10_cd60 + H24_sl60
```

建议风控：

```text
单币极小仓
单 timestamp cap 3~5
同币 cooldown 60m
hard stop -6%
最长持有 24h
日内 stop 过高时暂停新仓
继续记录 remove-top5、Sharpe、stop rate、same-ts random95
```

---

## 风险与限制

1. 当前热币 24h 执行验证主要是 proxy：

   ```text
   使用 return/MFE/MAE + stop-first barrier
   不是完整 tick/OHLC path replay
   ```

2. `-6%` hard stop 可能错过 V 型反弹，例如：

   ```text
   某些币最终 24h 大涨，但中途先触及 -6% stop。
   ```

3. 热门山寨币高度相关，不能简单把所有信号等权放大。

4. 最近 24h 的 core/night 仍有集中度风险：

   ```text
   OPN 对收益贡献较大；
   core/night remove-top5 后转弱。
   ```

5. 生产前仍需：

   ```text
   完整 OHLC replay
   live shadow/fresh-forward
   真实滑点/成交/限价与止损实现验证
   ```

---

## 结论

```text
做多雷达的 discovery/selector 有效；
执行层应围绕热门币 24h continuation，而不是 45m 短打；
当前最稳线上候选是 day_high_mkt10_cd60 + H24_sl60；
当前主 alpha 观察线是 core_mkt10_cd60 + H24_sl60；
上线应先 live shadow / paper，谨慎极小仓 canary，不建议直接 full production。
```
