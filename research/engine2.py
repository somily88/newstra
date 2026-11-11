import numpy as np, pandas as pd
from engine import load

def simulate(df, p, fee=0.001, start=None, end=None, return_trades=False):
    m = np.ones(len(df), bool)
    if start: m &= (df.time >= pd.Timestamp(start, tz="UTC")).values
    if end: m &= (df.time < pd.Timestamp(end, tz="UTC")).values
    idx = np.where(m)[0]
    O,H,L,C = (df[k].values for k in ("open","high","low","close"))
    E, V, E2, A, VS = df.ema.values, df.vwap.values, df.ema200.values, df.atr.values, df.vwap_slope.values
    trend = p.get("trend","none")
    up_ok = np.ones(len(df), bool); dn_ok = up_ok.copy()
    if trend in ("ema200","both"): up_ok = up_ok & (C > E2); dn_ok = dn_ok & (C < E2)
    if trend in ("vwap_slope","both"): up_ok = up_ok & (VS > 0); dn_ok = dn_ok & (VS < 0)
    sl = df.cross_up.values & up_ok
    ss = df.cross_down.values & dn_ok
    if p.get("confirm"): sl = sl & (C > E) & (C > V); ss = ss & (C < E) & (C < V)
    if p.get("min_atr_pct"): vol = A / C * 100 >= p["min_atr_pct"]; sl &= vol; ss &= vol
    sides = p.get("sides","long")
    if sides == "long": ss = np.zeros_like(ss)
    ex = p.get("exit","ema"); cd = p.get("cooldown",0)
    trades, pos, pend_in, pend_out, cool = [], None, 0, False, -1
    def close(i, price, why):
        nonlocal pos
        g = price/pos["e"] if pos["d"]==1 else 2-price/pos["e"]
        trades.append((i, pos["d"], g, why)); pos = None
    for i in idx[1:]:
        if pend_out and pos: close(i, O[i], "ema")
        pend_out = False
        if pend_in and pos is None:
            d, e, a = pend_in, O[i], A[i-1]
            if ex == "ema": st = e*(1-d*p.get("sl_pct",1.5)/100); tp = None
            else:
                st = e - d*p["atr_sl"]*a
                tp = e + d*p["tp_r"]*p["atr_sl"]*a if p.get("tp_r") else None
            pos = dict(d=d, e=e, st=st, tp=tp, ext=e)
        pend_in = 0
        if pos:
            d = pos["d"]
            if (d==1 and L[i] <= pos["st"]) or (d==-1 and H[i] >= pos["st"]):
                close(i, min(O[i],pos["st"]) if d==1 else max(O[i],pos["st"]), "stop"); cool = i+cd
            elif pos["tp"] and ((d==1 and H[i] >= pos["tp"]) or (d==-1 and L[i] <= pos["tp"])):
                close(i, max(O[i],pos["tp"]) if d==1 else min(O[i],pos["tp"]), "tp")
        if pos:
            d = pos["d"]
            if ex == "ema" and (C[i]-E[i])*d < 0: pend_out = True
            elif ex == "trail":
                if d==1: pos["ext"]=max(pos["ext"],C[i]); pos["st"]=max(pos["st"], pos["ext"]-p["trail"]*A[i])
                else: pos["ext"]=min(pos["ext"],C[i]); pos["st"]=min(pos["st"], pos["ext"]+p["trail"]*A[i])
        if (pos is None or pend_out) and i >= cool:
            if sl[i]: pend_in = 1
            elif ss[i]: pend_in = -1
    if pos: close(idx[-1], C[idx[-1]], "end")
    g = np.array([t[2] for t in trades]); r = g*(1-fee)**2 - 1 if len(g) else np.array([])
    eq = np.cumprod(np.r_[1, 1+r]); dd = (eq/np.maximum.accumulate(eq)-1).min()*100
    gp, gl = r[r>0].sum(), -r[r<=0].sum()
    res = dict(trades=len(r), win=(r>0).mean()*100 if len(r) else 0, ret=(eq[-1]-1)*100,
               pf=gp/gl if gl>0 else np.inf, mdd=dd, bh=(C[idx[-1]]/O[idx[0]]-1)*100)
    return (res, trades) if return_trades else res
