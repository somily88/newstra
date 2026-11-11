import ccxt, pandas as pd, sys, time, os
from datetime import datetime, timezone, timedelta
sym, tf = sys.argv[1], sys.argv[2]
ex = ccxt.binance({"enableRateLimit": True})
end = int(datetime(2026,11,11,0,0,tzinfo=timezone.utc).timestamp()*1000)
start = int(datetime(2025,11,10,0,0,tzinfo=timezone.utc).timestamp()*1000)  # 1 warmup day before 2025-11-11
step = ex.parse_timeframe(tf)*1000
rows, since = [], start
while since < end:
    for a in range(6):
        try: b = ex.fetch_ohlcv(sym, tf, since=since, limit=1000); break
        except Exception as e: time.sleep(2**a)
    if not b: break
    rows += [r for r in b if r[0] < end]; since = b[-1][0] + step
df = pd.DataFrame(rows, columns=["ts","open","high","low","close","volume"]).drop_duplicates("ts")
out = os.path.join(os.path.dirname(os.path.abspath(__file__)), "") + f"{sym.replace('/','')}_{tf}.csv"; df.to_csv(out, index=False); print(out, len(df))
