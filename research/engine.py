import sys, numpy as np, pandas as pd
import os; sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), ".."))
from strategy import add_indicators, ohlcv_to_df

def load(sym, tf):
    raw = pd.read_csv(os.path.join(os.path.dirname(os.path.abspath(__file__)), f"{sym}_{tf}.csv")).values.tolist()
    df = add_indicators(ohlcv_to_df(raw), 9, "UTC", "00:00")
    df["ema200"] = df.close.ewm(span=200, adjust=False).mean()
    pc = df.close.shift(1)
    tr = pd.concat([df.high-df.low, (df.high-pc).abs(), (df.low-pc).abs()], axis=1).max(axis=1)
    df["atr"] = tr.ewm(alpha=1/14, adjust=False).mean()
    df["vwap_slope"] = df.vwap - df.vwap.shift(5)
    return df

def simulate(df, p, fee=0.001, start=None, end=None):
    m = np.ones(len(df), bool)
    if start: m &= (df.time >= pd.Timestamp(start, tz="UTC")).values
    if end: m &= (df.time < pd.Timestamp(end, tz="UTC")).values
    idx = np.where(m)[0]
    O,H,L,C = (df[k].values for k in ("open","high","low","close"))
    E, V, E2, A, VS = df.ema.values, df.vwap.values, df.ema200.values, df.atr.values, df.vwap_slope.values
    X = df.cross_up.values
    trend = p.get("trend","none")
    ok = np.ones(len(df), bool)
    if trend == "ema200": ok = C > E2
    elif trend == "vwap_slope": ok = VS > 0
    elif trend == "both": ok = (C > E2) & (VS > 0)
    sig = X & ok
    if p.get("confirm"): sig &= (C > E) & (C > V)
    exit_mode = p.get("exit","ema")
    rets, pos, pend_in, pend_out, cool_until = [], None, False, False, -1
    for i in idx[1:] if len(idx) else []:
        if pend_out and pos:
            rets.append(O[i]/pos["e"]); pos = None
        pend_out = False
        if pend_in and pos is None:
            e = O[i]; a = A[i-1]
            if exit_mode == "ema":
                st, tp = e*(1-p.get("sl_pct",1.5)/100), None
            else:
                st = e - p["atr_sl"]*a
                tp = e + p["tp_r"]*p["atr_sl"]*a if p.get("tp_r") else None
            pos = {"e": e, "st": st, "tp": tp, "hi": e, "a": a}
        pend_in = False
        if pos:
            if L[i] <= pos["st"]:
                rets.append(min(O[i], pos["st"])/pos["e"]); pos=None; cool_until = i + p.get("cooldown",0)
            elif pos["tp"] and H[i] >= pos["tp"]:
                rets.append(max(O[i], pos["tp"])/pos["e"]); pos=None
        if pos:
            if exit_mode == "ema" and C[i] < E[i]: pend_out = True
            elif exit_mode == "trail":
                pos["hi"] = max(pos["hi"], C[i])
                pos["st"] = max(pos["st"], pos["hi"] - p["trail"]*A[i])
                if p.get("trail_ema") and C[i] < E[i]: pass
            elif exit_mode == "atr" and p.get("ema_exit") and C[i] < E[i]: pend_out = True
        if (pos is None or pend_out) and sig[i] and i >= cool_until:
            pend_in = True
    if pos: rets.append(C[idx[-1]]/pos["e"])
    r = np.array(rets)*(1-fee)**2 - 1
    eq = np.cumprod(np.r_[1, 1+r])
    dd = (eq/np.maximum.accumulate(eq)-1).min()*100
    gp, gl = r[r>0].sum(), -r[r<=0].sum()
    bh = (C[idx[-1]]/O[idx[0]]-1)*100
    return dict(trades=len(r), win=(r>0).mean()*100 if len(r) else 0, ret=(eq[-1]-1)*100,
                pf=gp/gl if gl>0 else np.inf, mdd=dd, bh=bh, avg=r.mean()*100 if len(r) else 0)
