#!/usr/bin/env python3
from __future__ import annotations

import json
import math
import random
import sys
from collections import Counter, defaultdict
from datetime import datetime, timezone, timedelta
from pathlib import Path
from typing import Any, Mapping, Sequence

PROJECT_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PROJECT_ROOT / "src"))
from radar_alpha_skills_lab.old_radar_alpha import load_old_radar_rows  # noqa: E402
from radar_alpha_skills_lab.signal_control import COSTS, iso, pct, stat  # noqa: E402

OUT = PROJECT_ROOT / "output"
BJ = timezone(timedelta(hours=8))
COST = float(COSTS.get("all_taker", 0.0008))
SIMS = 800
CORE_HOURS = set(list(range(20,24))+list(range(0,8)))
NIGHT_HOURS = set(list(range(20,24))+list(range(0,4)))

POOLS = []
EXITS = [
    # name, horizon, mode, tp/sl/lock
    ("H4_hold", "4h", "hold", None, None, None),
    ("H24_hold", "24h", "hold", None, None, None),
    ("H4_sl35", "4h", "hold_sl", None, -0.035, None),
    ("H24_sl60", "24h", "hold_sl", None, -0.060, None),
    ("TP20_SL35_4h", "4h", "tp_sl", 0.020, -0.035, None),
    ("TP30_SL50_24h", "24h", "tp_sl", 0.030, -0.050, None),
    ("TP50_SL70_24h", "24h", "tp_sl", 0.050, -0.070, None),
    ("HALF20_REST24h_SL50", "24h", "split", 0.020, -0.050, 0.0),
    ("HALF30_REST24h_SL60", "24h", "split", 0.030, -0.060, 0.0),
]


def _num(x: Any) -> float:
    try:
        v=float(x or 0.0); return v if math.isfinite(v) else 0.0
    except Exception: return 0.0


def top_frac(rows: Sequence[Mapping[str, Any]], key: str, frac: float) -> list[dict[str, Any]]:
    by=defaultdict(list)
    for r in rows: by[r['ts_dt']].append(dict(r))
    out=[]
    for _ts,g in sorted(by.items()):
        n=max(1, math.ceil(len(g)*frac))
        out.extend(sorted(g, key=lambda r:(-_num(r.get(key)), str(r.get('symbol'))))[:n])
    return out


def cooldown(rows: Sequence[Mapping[str, Any]], minutes:int) -> list[dict[str, Any]]:
    last={}; out=[]
    for r in sorted([dict(x) for x in rows], key=lambda x:(x['ts_dt'], -_num(x.get('market_confirmation_score')), str(x.get('symbol')))):
        s=str(r.get('symbol'))
        if s in last and (r['ts_dt']-last[s]).total_seconds() < minutes*60: continue
        last[s]=r['ts_dt']; out.append(r)
    return out


def cap_ts(rows, cap):
    by=defaultdict(list)
    for r in rows: by[r['ts_dt']].append(dict(r))
    out=[]
    for _ts,g in sorted(by.items()):
        out.extend(sorted(g, key=lambda r:(-_num(r.get('market_confirmation_score')), -_num(r.get('momentum_confirmation_score')), str(r.get('symbol'))))[:cap])
    return out


def build():
    rows, meta=load_old_radar_rows()
    rows=[dict(r) for r in rows if r.get('return_24h') is not None]
    universes={
        'all': rows,
        'core': [r for r in rows if int(r.get('hour_bjt') or 0) in CORE_HOURS],
        'night': [r for r in rows if int(r.get('hour_bjt') or 0) in NIGHT_HOURS],
        'wait_entry': [r for r in rows if str(r.get('recommended_action'))=='wait_for_entry_trigger'],
        'watch_hot': [r for r in rows if str(r.get('decision_status'))=='watch_hot'],
        'day_high': [r for r in rows if str(r.get('session'))=='day_high_threshold'],
        'prewarm': [r for r in rows if str(r.get('session'))=='prewarm'],
    }
    pools={}
    for name,u in universes.items():
        if len(u)<50: continue
        for frac,label in [(0.10,'mkt10'),(0.20,'mkt20'),(0.33,'mkt33')]:
            sel=cooldown(top_frac(u,'market_confirmation_score',frac),60)
            pools[f'{name}_{label}_cd60']=(cap_ts(u,25), cap_ts(sel,8))
    return pools, meta


def exec_pnl(r, exit_spec):
    name,h,mode,tp,sl,lock=exit_spec
    ret=_num(r.get(f'return_{h}'))
    mfe=_num(r.get(f'mfe_{h}'))
    mae=_num(r.get(f'mae_{h}'))
    if mode=='hold': return ret-COST, 'time_exit'
    if mode=='hold_sl':
        if mae <= float(sl): return float(sl)-COST, 'hard_sl'
        return ret-COST, 'time_exit'
    if mode=='tp_sl':
        # Conservative: if both stop and target are seen in horizon, assume stop first.
        if mae <= float(sl): return float(sl)-COST, 'hard_sl'
        if mfe >= float(tp): return float(tp)-COST, 'tp'
        return ret-COST, 'time_exit'
    if mode=='split':
        if mae <= float(sl): return float(sl)-COST, 'hard_sl'
        if mfe >= float(tp): return 0.5*float(tp) + 0.5*ret - COST, 'half_tp_rest_time'
        return ret-COST, 'time_exit'
    return ret-COST, 'time_exit'


def simulate(rows, exit_spec):
    out=[]
    for r0 in rows:
        r=dict(r0); pnl, reason=exec_pnl(r, exit_spec); r['pnl']=pnl; r['reason']=reason; out.append(r)
    return out


def q(xs,p):
    if not xs: return 0.0
    ys=sorted(xs); return ys[min(len(ys)-1,max(0,int(round((len(ys)-1)*p))))]


def rand_same_ts(univ, sel, seed):
    u_by=defaultdict(list); s_by=defaultdict(list)
    for r in univ: u_by[r['ts_dt']].append(r)
    for r in sel: s_by[r['ts_dt']].append(r)
    rng=random.Random(seed); av=[]; su=[]; sh=[]
    for _ in range(SIMS):
        vals=[]
        for ts,sg in s_by.items():
            pool=u_by.get(ts, [])
            if not pool: continue
            n=len(sg); sample=pool if n>=len(pool) else rng.sample(pool,n)
            vals.extend(float(x['pnl']) for x in sample)
        st=stat(vals); av.append(st['avg']); su.append(st['sum']); sh.append(st['sharpe_like'])
    return {'avg_p95':q(av,.95),'sum_p95':q(su,.95),'sh_p95':q(sh,.95)}


def cap_comp(rows, cap):
    by=defaultdict(list)
    for r in rows: by[r['ts_dt']].append(r)
    vals=[]
    for _ts,g in sorted(by.items()): vals.extend(float(r['pnl']) for r in sorted(g,key=lambda r:(-_num(r.get('market_confirmation_score')),str(r.get('symbol'))))[:cap])
    comp=1.0
    for v in vals: comp*=1+v
    st=stat(vals); return {'n':len(vals),'avg':st['avg'],'sum':st['sum'],'sh':st['sharpe_like'],'comp':comp-1}


def summarize(pool, exit_spec, selected_n, rows, rand):
    vals=[float(r['pnl']) for r in rows]; st=stat(vals)
    days=defaultdict(list); bysym=defaultdict(float); ns=Counter()
    for r in rows:
        days[r['ts_dt'].astimezone(BJ).date().isoformat()].append(float(r['pnl']))
        s=str(r.get('symbol')); bysym[s]+=float(r['pnl']); ns[s]+=1
    top=sorted(bysym.items(), key=lambda kv:kv[1], reverse=True)[:5]
    rem=[float(r['pnl']) for r in rows if str(r.get('symbol')) not in {s for s,_ in top}]
    remst=stat(rem)
    out={'pool':pool,'exit':exit_spec[0],'n':len(rows),'selected_n':selected_n,'avg':st['avg'],'sum':st['sum'],'sh':st['sharpe_like'],'rand_avg_p95':rand['avg_p95'],'rand_sum_p95':rand['sum_p95'],'rand_sh_p95':rand['sh_p95'],'edge_avg':st['avg']-rand['avg_p95'],'edge_sum':st['sum']-rand['sum_p95'],'cap5':cap_comp(rows,5),'cap10':cap_comp(rows,10),'rem_top5_avg':remst['avg'],'rem_top5_sum':remst['sum'],'pos_days':sum(1 for v in days.values() if sum(v)>0),'days':len(days),'stop_rate':sum(1 for r in rows if str(r['reason'])=='hard_sl')/max(1,len(rows)),'reasons':dict(Counter(str(r['reason']) for r in rows)),'top_symbols':[{'symbol':s,'pnl':v,'n':ns[s]} for s,v in top]}
    out['score']=out['edge_avg']*10000 + (out['pos_days']/max(1,out['days']))*5 + out['rem_top5_avg']*2000 + min(5,out['sh']) - max(0,out['stop_rate']-.35)*5
    return out


def render(meta, results):
    lines=['# Fast hotcoin execution proxy search','',f'generated_utc: `{iso(datetime.now(timezone.utc))}`','', 'Proxy uses old-radar DB return/MFE/MAE horizons, conservative stop-first barrier logic, same-ts random p95, all-taker 8bp. This is for finding execution geometry before slower OHLC replay.','', '## Meta','```text',json.dumps(meta,ensure_ascii=False,indent=2,default=str),'```','','## Results','```text']
    for r in results[:40]:
        lines.append(f"score={r['score']:7.2f} {r['pool']:<24} {r['exit']:<22} n={r['n']:4d}/{r['selected_n']:<4d} avg={pct(r['avg']):>8}/{pct(r['rand_avg_p95']):>8} edge={pct(r['edge_avg']):>8} sum={pct(r['sum']):>9}/{pct(r['rand_sum_p95']):>9} sh={r['sh']:5.2f}/{r['rand_sh_p95']:5.2f} cap5={pct(r['cap5']['comp']):>9} remT5={pct(r['rem_top5_avg']):>8}/{pct(r['rem_top5_sum']):>9} days={r['pos_days']}/{r['days']} stop={r['stop_rate']*100:4.1f}%")
        lines.append('  reasons='+json.dumps(r['reasons'],ensure_ascii=False)+' top='+', '.join(f"{x['symbol']} {pct(x['pnl'])}/{x['n']}" for x in r['top_symbols']))
    lines += ['```','','## Verdict','```text']
    passes=[r for r in results if r['edge_avg']>0 and r['rem_top5_avg']>0 and r['pos_days']>=int(r['days']*.65)]
    if passes:
        b=passes[0]; lines.append(f"FOUND proxy execution geometry: {b['pool']} + {b['exit']}. Use it as the next OHLC replay target, not production yet.")
    else:
        lines.append('No full-window proxy execution geometry passed strict gates.')
    lines += ['```','']
    return '\n'.join(lines)


def main():
    pools, meta=build(); results=[]; i=0
    for pname,(univ,sel) in sorted(pools.items()):
        if len(sel)<30: continue
        for ex in EXITS:
            us=simulate(univ,ex); ss=simulate(sel,ex); rand=rand_same_ts(us,ss,2026060600+i); i+=1
            results.append(summarize(pname,ex,len(sel),ss,rand))
    results.sort(key=lambda r:r['score'], reverse=True)
    result={'generated_utc':iso(datetime.now(timezone.utc)),'meta':meta,'results':results}
    ts=datetime.now().strftime('%Y%m%d-%H%M%S')
    jp=OUT/f'hotcoin-execution-proxy-search-{ts}.json'; mp=OUT/f'hotcoin-execution-proxy-search-{ts}.md'; lj=OUT/'hotcoin-execution-proxy-search-latest.json'; lm=OUT/'hotcoin-execution-proxy-search-latest.md'
    md=render(meta,results)
    for p in [jp,lj]: p.write_text(json.dumps(result,ensure_ascii=False,indent=2,default=str)+'\n')
    for p in [mp,lm]: p.write_text(md+'\n')
    print(jp); print(mp); print(lj); print(lm)

if __name__=='__main__': main()
