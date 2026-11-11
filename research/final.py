import pandas as pd
from engine import load
from engine2 import simulate
P = {"ORIGINAL video (3m)": ("3m", dict(sides="long", exit="ema", sl_pct=1.5)),
     "A: 1h trend (winner)": ("1h", dict(sides="long", trend="ema200", confirm=True, exit="ema", sl_pct=1.5)),
     "B: 1h trend+VWAP slope": ("1h", dict(sides="long", trend="both", confirm=True, exit="ema", sl_pct=1.5)),
     "C: 1h long+short ATR (futures)": ("1h", dict(sides="both", trend="ema200", exit="atr", atr_sl=2.5, tp_r=3)),}
pers = {"IS": ("2025-11-11","2026-07-11"), "OOS": ("2026-07-11","2026-11-11"), "FULL": ("2025-11-11","2026-11-11")}
cache = {}
for name,(tf,p) in P.items():
    for s in ("BTCUSDT","ETHUSDT"):
        df = cache.setdefault((s,tf), load(s,tf))
        for per,(a,b) in pers.items():
            for fee in (0.001,0.00075):
                r = simulate(df,p,fee,a,b)
                print(f"{name:32s} {s} {per:4s} fee{fee*100:.3f}% trades {r['trades']:4d} win {r['win']:5.1f}% ret {r['ret']:+7.2f}% pf {r['pf']:.2f} mdd {r['mdd']:6.2f}% bh {r['bh']:+7.2f}%")
df = cache[("BTCUSDT","1h")]; print("BTC 1h range", df.time.iloc[0], df.time.iloc[-1], len(df))
df = cache[("BTCUSDT","3m")]; print("BTC 3m range", df.time.iloc[0], df.time.iloc[-1], len(df))
# sensitivity of winner
import numpy as np
for s in ("BTCUSDT","ETHUSDT"):
    base = cache[(s,"1h")]
    for L in (100,200,300):
        d = base.copy(); d["ema200"] = d.close.ewm(span=L, adjust=False).mean()
        for sl in (1.0,1.5,2.5):
            ri = simulate(d, dict(sides="long",trend="ema200",confirm=True,exit="ema",sl_pct=sl), 0.001, *pers["IS"])
            ro = simulate(d, dict(sides="long",trend="ema200",confirm=True,exit="ema",sl_pct=sl), 0.001, *pers["OOS"])
            print(f"sens {s} EMA{L} sl{sl}: IS {ri['trades']} {ri['ret']:+.2f}% pf {ri['pf']:.2f} | OOS {ro['trades']} {ro['ret']:+.2f}% pf {ro['pf']:.2f}")
