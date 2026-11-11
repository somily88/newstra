"""
Read-only web dashboard for the Binance VWAP bot.

    python dashboard.py            # then open http://localhost:8765

It only READS files the bot writes (status.json, trades CSV, log file, backtest
reports) and public Binance price data for the chart. It has NO API keys and
cannot place or cancel orders.

Optional password: set DASHBOARD_PASSWORD=something (any username works).
"""
from __future__ import annotations

import base64
import csv
import glob
import json
import os
import secrets
import threading
import time
from collections import deque
from datetime import datetime, timezone

import ccxt
import yaml
from fastapi import FastAPI, HTTPException, Query, Request
from fastapi.responses import FileResponse, JSONResponse, Response

from strategy import add_indicators, ohlcv_to_df, resolve_anchor, resolve_params

HERE = os.path.dirname(os.path.abspath(__file__))
CONFIG_FILE = os.getenv("BOT_CONFIG", os.path.join(HERE, "config.yaml"))
PASSWORD = os.getenv("DASHBOARD_PASSWORD", "")

# Never let this process see trading keys, even if they are in the environment.
for _k in ("BINANCE_API_KEY", "BINANCE_API_SECRET"):
    os.environ.pop(_k, None)


def p(path_: str) -> str:
    return path_ if os.path.isabs(path_) else os.path.join(HERE, path_)


def load_cfg() -> dict:
    with open(CONFIG_FILE) as fh:
        return yaml.safe_load(fh) or {}


def files(cfg: dict, status: dict | None = None) -> dict:
    state = p(os.getenv("STATE_FILE") or cfg.get("state_file", "state.json"))
    out = {
        "status": p(os.getenv("STATUS_FILE") or os.path.join(os.path.dirname(state), "status.json")),
        "trades": p(os.getenv("TRADE_LOG_CSV") or cfg.get("trade_log_csv", "logs/trades.csv")),
        "log": p(os.getenv("LOG_FILE") or cfg.get("log_file", "logs/bot.log")),
    }
    f = (status or {}).get("files") or {}
    if f.get("trades_csv") and os.path.exists(f["trades_csv"]):
        out["trades"] = f["trades_csv"]
    if f.get("log_file") and os.path.exists(f["log_file"]):
        out["log"] = f["log_file"]
    return out


def read_status(cfg: dict) -> dict | None:
    path_ = files(cfg)["status"]
    if not os.path.exists(path_):
        return None
    try:
        with open(path_) as fh:
            return json.load(fh)
    except Exception:
        return None


app = FastAPI(title="Binance VWAP bot dashboard", docs_url=None, redoc_url=None, openapi_url=None)


@app.middleware("http")
async def basic_auth(request: Request, call_next):
    if PASSWORD and request.url.path != "/api/health":
        ok = False
        hdr = request.headers.get("authorization", "")
        if hdr.lower().startswith("basic "):
            try:
                _, _, pw = base64.b64decode(hdr[6:]).decode().partition(":")
                ok = secrets.compare_digest(pw, PASSWORD)
            except Exception:
                ok = False
        if not ok:
            return Response("Password required", status_code=401,
                            headers={"WWW-Authenticate": 'Basic realm="bot dashboard"'})
    resp = await call_next(request)
    resp.headers["Cache-Control"] = "no-store"
    resp.headers["X-Content-Type-Options"] = "nosniff"
    return resp


@app.get("/")
def index():
    return FileResponse(os.path.join(HERE, "web", "index.html"))


@app.get("/api/health")
def health():
    return {"ok": True, "time_utc": datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S UTC")}


@app.get("/api/status")
def api_status():
    cfg = load_cfg()
    st = read_status(cfg)
    params = resolve_params(cfg)
    configured = {
        "mode": str(cfg.get("mode", "paper")).lower(), "strategy": params["name"],
        "timeframe": params["timeframe"], "symbols": cfg.get("symbols") or ["BTC/USDT"],
        "market_type": cfg.get("market_type", "spot"), "trade_usdt": cfg.get("trade_usdt"),
    }
    if not st:
        return {"has_status": False, "running": False, "configured": configured}
    age = time.time() - st.get("heartbeat_ms", 0) / 1000
    st.update(has_status=True, configured=configured, heartbeat_age_s=round(age, 1),
              running=st.get("phase") != "stopped" and age < 90)
    st.pop("files", None)
    st.pop("pid", None)
    return st


def read_trades(cfg: dict) -> list[dict]:
    path_ = files(cfg, read_status(cfg))["trades"]
    if not os.path.exists(path_):
        return []
    with open(path_, newline="") as fh:
        return list(csv.DictReader(fh))


def fnum(x):
    try:
        return float(x)
    except (TypeError, ValueError):
        return None


@app.get("/api/trades")
def api_trades():
    cfg = load_cfg()
    rows = read_trades(cfg)
    closes = [r for r in rows if r.get("action") == "CLOSE"]
    pnls = [fnum(r.get("pnl_usdt")) or 0.0 for r in closes]
    wins = sum(1 for x in pnls if x > 0)
    stats = {
        "total_trades": len(closes), "open_orders_logged": sum(1 for r in rows if r.get("action") == "OPEN"),
        "wins": wins, "losses": len(closes) - wins,
        "win_rate_pct": round(wins / len(closes) * 100, 2) if closes else None,
        "total_pnl_usdt": round(sum(pnls), 6), "best_usdt": round(max(pnls), 6) if pnls else None,
        "worst_usdt": round(min(pnls), 6) if pnls else None,
        "total_fees_usdt": round(sum(fnum(r.get("fee_usdt")) or 0 for r in rows), 6),
    }
    return {"stats": stats, "trades": rows[-500:][::-1]}


@app.get("/api/logs")
def api_logs(n: int = Query(120, ge=1, le=1000)):
    cfg = load_cfg()
    path_ = files(cfg, read_status(cfg))["log"]
    if not os.path.exists(path_):
        return {"lines": []}
    with open(path_, encoding="utf-8", errors="replace") as fh:
        return {"lines": [l.rstrip("\n") for l in deque(fh, maxlen=n)]}


@app.get("/api/backtest")
def api_backtest(name: str | None = None):
    reports = sorted(glob.glob(os.path.join(HERE, "data", "backtest_report_*.txt")),
                     key=os.path.getmtime, reverse=True)
    names = [os.path.basename(r) for r in reports]
    if not reports:
        return {"reports": [], "name": None, "text": None}
    pick = reports[names.index(name)] if name in names else reports[0]
    with open(pick, encoding="utf-8") as fh:
        text = fh.read()
    mtime = datetime.fromtimestamp(os.path.getmtime(pick), timezone.utc).strftime("%Y-%m-%d %H:%M UTC")
    return {"reports": names, "name": os.path.basename(pick), "modified_utc": mtime, "text": text}


# ---- public market data for the chart (no keys) ----
_ex_lock = threading.Lock()
_exchanges: dict[str, ccxt.Exchange] = {}
_cache: dict[tuple, tuple[float, dict]] = {}


def public_exchange(market_type: str) -> ccxt.Exchange:
    with _ex_lock:
        if market_type not in _exchanges:
            cls = ccxt.binanceusdm if market_type == "futures" else ccxt.binance
            ex = cls({"enableRateLimit": True})
            ex.load_markets()
            _exchanges[market_type] = ex
        return _exchanges[market_type]


@app.get("/api/candles")
def api_candles(symbol: str | None = None, limit: int = Query(300, ge=50, le=1000)):
    cfg = load_cfg()
    params = resolve_params(cfg)
    mt = str(cfg.get("market_type", "spot")).lower()
    symbols = cfg.get("symbols") or ["BTC/USDT"]
    symbol = symbol or symbols[0]
    sym = symbol if (mt != "futures" or ":" in symbol) else f"{symbol}:{symbol.split('/')[1]}"
    key = (sym, params["timeframe"], limit, mt)
    hit = _cache.get(key)
    if hit and time.time() - hit[0] < 15:
        return hit[1]
    try:
        ex = public_exchange(mt)
        # fetch extra history so EMA200 and the VWAP session are warmed up
        tf_ms = ex.parse_timeframe(params["timeframe"]) * 1000
        since = int(time.time() * 1000) - (limit + 600) * tf_ms
        raw = []
        while True:
            batch = ex.fetch_ohlcv(sym, params["timeframe"], since=since, limit=1000)
            raw += batch
            if len(batch) < 1000:
                break
            since = batch[-1][0] + tf_ms
    except Exception as e:
        raise HTTPException(502, f"Could not load public candles from Binance: {type(e).__name__}: {str(e)[:200]}")
    tz, at = resolve_anchor(cfg)
    df = add_indicators(ohlcv_to_df(raw), int(params["ema_period"]), tz, at, params=params).tail(limit)
    tf_s = ex.parse_timeframe(params["timeframe"])
    bars = [{"time": int(r.ts // 1000), "open": r.open, "high": r.high, "low": r.low, "close": r.close,
             "ema": round(r.ema, 4), "vwap": round(r.vwap, 4), "ema_trend": round(r.ema_trend, 4),
             "long_signal": bool(r.long_signal)} for r in df.itertuples()]
    # buy/sell markers from the trade log, snapped to the candle they happened in
    markers = []
    first = bars[0]["time"] if bars else 0
    for t in read_trades(cfg):
        if t.get("symbol") not in (symbol, sym):
            continue
        try:
            ts = int(datetime.strptime(t["time_utc"], "%Y-%m-%d %H:%M:%S UTC")
                     .replace(tzinfo=timezone.utc).timestamp())
        except Exception:
            continue
        ts = ts // tf_s * tf_s
        if ts < first:
            continue
        buy = (t.get("action") == "OPEN") == (t.get("side") == "long")
        markers.append({"time": ts, "action": t.get("action"), "side": t.get("side"), "buy": buy,
                        "price": fnum(t.get("price")), "pnl_usdt": fnum(t.get("pnl_usdt")),
                        "reason": t.get("reason")})
    data = {"symbol": symbol, "timeframe": params["timeframe"], "source": f"Binance public API ({mt})",
            "bars": bars, "markers": sorted(markers, key=lambda m: m["time"])}
    _cache[key] = (time.time(), data)
    return data


@app.exception_handler(FileNotFoundError)
async def nf(_, exc):
    return JSONResponse({"detail": str(exc)}, status_code=404)


if __name__ == "__main__":
    import uvicorn
    host = os.getenv("DASHBOARD_HOST", "127.0.0.1")
    port = int(os.getenv("DASHBOARD_PORT", "8765"))
    print(f"Dashboard: http://{'localhost' if host in ('127.0.0.1', '0.0.0.0') else host}:{port}"
          f"{'  (password protected)' if PASSWORD else ''}")
    uvicorn.run(app, host=host, port=port, log_level="warning")
