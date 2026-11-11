import itertools, pandas as pd
from engine import load
from engine2 import simulate
IS = ("2025-11-11","2026-07-11"); OOS = ("2026-07-11","2026-11-11")
exits = [dict(exit="ema", sl_pct=1.5)] + [dict(exit="atr", atr_sl=s, tp_r=t) for s in (1.5,2.5) for t in (1.5,3)] + [dict(exit="trail", atr_sl=k, trail=k) for k in (2,3)]
rows=[]
for tf in ("3m","5m","15m","1h"):
    data = {s: load(s, tf) for s in ("BTCUSDT","ETHUSDT")}
    for sides, trend, conf, ex, cd in itertools.product(("long","both"), ("none","ema200","vwap_slope","both"), (False,True), exits, (0,)):
        p = dict(sides=sides, trend=trend, confirm=conf, cooldown=cd, **ex)
        row = dict(tf=tf, **p)
        for s, df in data.items():
            for per,(a,b) in (("IS",IS),("OOS",OOS)):
                for k,v in simulate(df, p, 0.001, a, b).items(): row[f"{s[:3]}_{per}_{k}"]=v
        rows.append(row)
    print(tf, flush=True)
pd.DataFrame(rows).to_csv("grid2.csv", index=False)
