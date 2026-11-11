"""
Backtest the VWAP + EMA9 strategy on historical Binance candles.

Examples:
    python backtest.py                              # BTC/USDT, last 60 days (config.yaml)
    python backtest.py --symbol ETH/USDT --days 30
    python backtest.py --start 2026-09-01 --end 2026-10-31
    python backtest.py --short                      # also take short trades
"""
from __future__ import annotations

import argparse
import os
import time
from datetime import datetime, timedelta, timezone

import ccxt
import numpy as np
import pandas as pd
import yaml

from strategy import (add_indicators, exit_signal, initial_stops, ohlcv_to_df, resolve_anchor,
                      resolve_params, update_trailing)

HERE = os.path.dirname(os.path.abspath(__file__))


def tf_ms(tf: str) -> int:
    return ccxt.Exchange.parse_timeframe(tf) * 1000


def make_sources():
    """Data sources tried in order. First is normal Binance, then Binance's
    public market-data mirror, then other exchanges as a last resort."""
    srcs = []
    srcs.append(("binance (api.binance.com)", ccxt.binance({"enableRateLimit": True})))
    mirror = ccxt.binance({"enableRateLimit": True})
    for k in list(mirror.urls["api"].keys()):
        v = mirror.urls["api"][k]
        if isinstance(v, str) and "api.binance.com/api" in v:
            mirror.urls["api"][k] = v.replace("api.binance.com", "data-api.binance.vision")
    srcs.append(("binance mirror (data-api.binance.vision)", mirror))
    srcs.append(("FALLBACK okx", ccxt.okx({"enableRateLimit": True})))
    srcs.append(("FALLBACK bybit", ccxt.bybit({"enableRateLimit": True})))
    return srcs


def download(symbol: str, timeframe: str, start_ms: int, end_ms: int):
    step = tf_ms(timeframe)
    last_err = None
    for name, ex in make_sources():
        try:
            rows, since = [], start_ms
            while since < end_ms:
                batch = None
                for attempt in range(5):
                    try:
                        batch = ex.fetch_ohlcv(symbol, timeframe, since=since, limit=1000)
                        break
                    except (ccxt.NetworkError, ccxt.ExchangeNotAvailable) as e:
                        time.sleep(2 ** attempt)
                        last_err = e
                if batch is None:
                    raise RuntimeError(f"too many network errors: {last_err}")
                if not batch:
                    break
                rows.extend(b for b in batch if b[0] < end_ms)
                nxt = batch[-1][0] + step
                if nxt <= since:
                    break
                since = nxt
                print(f"\r  [{name}] downloaded {len(rows)} candles ...", end="", flush=True)
            print()
            if rows:
                return name, rows
        except Exception as e:  # geo-block, unknown symbol, etc. -> try next source
            print(f"  source {name} failed: {type(e).__name__}: {str(e)[:150]}")
            last_err = e
    raise RuntimeError(f"Could not download data from any source: {last_err}")


def run_backtest(df: pd.DataFrame, p: dict, fee_pct: float, slip_pct: float,
                 allow_short: bool, start_balance: float, trade_usdt: float):
    """Candle-by-candle simulation. Signals are decided on a candle CLOSE and
    filled at the NEXT candle's open. Stops/targets are checked inside each
    candle (stop first if both are touched = conservative)."""
    f, s = fee_pct / 100, slip_pct / 100
    O, H, L, C = (df[k].to_numpy() for k in ("open", "high", "low", "close"))
    E, A = df["ema"].to_numpy(), df["atr"].to_numpy()
    LS = df["long_signal"].to_numpy()
    SS = df["short_signal"].to_numpy() if allow_short else np.zeros(len(df), bool)
    cooldown = int(p.get("cooldown_bars") or 0)

    trades, pos = [], None
    pending_exit, pending_entry, cool_until = None, None, -1
    equity, fixed_pnl, curve = start_balance, 0.0, []

    def close_pos(i, price, reason):
        nonlocal pos, equity, fixed_pnl
        side = pos["side"]
        if side == "long":
            fill = price * (1 - s)
            gross = fill / pos["entry"]
        else:
            fill = price * (1 + s)
            gross = 2 - fill / pos["entry"]
        ret = gross * (1 - f) * (1 - f) - 1   # fee on entry and exit
        equity *= 1 + ret
        fixed_pnl += trade_usdt * ret
        trades.append({
            "side": side, "entry_time": df["time"].iat[pos["i"]], "entry": pos["entry"],
            "exit_time": df["time"].iat[i], "exit": fill, "reason": reason,
            "return_pct": ret * 100, "bars": i - pos["i"],
        })
        pos = None

    for i in range(1, len(df)):
        o, h, l, c = O[i], H[i], L[i], C[i]
        # 1) orders decided at previous candle close fill at this candle's open
        if pending_exit and pos:
            close_pos(i, o, pending_exit)
        pending_exit = None
        if pending_entry and pos is None:
            side = pending_entry
            entry = o * (1 + s) if side == "long" else o * (1 - s)
            stop, tp = initial_stops(side, entry, A[i - 1], p)
            pos = {"side": side, "entry": entry, "i": i, "stop": stop, "tp": tp}
        pending_entry = None

        # 2) stop loss / take profit inside the candle
        if pos:
            st, tp = pos["stop"], pos["tp"]
            if pos["side"] == "long":
                if st and l <= st:
                    close_pos(i, min(o, st), "stop_loss"); cool_until = i + cooldown
                elif tp and h >= tp:
                    close_pos(i, max(o, tp), "take_profit")
            else:
                if st and h >= st:
                    close_pos(i, max(o, st), "stop_loss"); cool_until = i + cooldown
                elif tp and l <= tp:
                    close_pos(i, min(o, tp), "take_profit")

        # 3) on candle close: exits, trailing stop, new signals
        if pos:
            if p["exit_mode"] == "ema" and exit_signal(pos["side"], c, E[i]):
                pending_exit = "close_through_ema"
            elif p["exit_mode"] == "trail":
                pos["stop"] = update_trailing(pos["side"], pos, c, A[i], p)
        if (pos is None or pending_exit) and i >= cool_until:
            if LS[i]:
                pending_entry = "long"
            elif SS[i]:
                pending_entry = "short"
        curve.append(equity)

    if pos:
        close_pos(len(df) - 1, C[-1], "end_of_data")
        curve[-1] = equity
    return trades, curve, equity, fixed_pnl


def max_drawdown(curve):
    peak, mdd = -1e18, 0.0
    for v in curve:
        peak = max(peak, v)
        mdd = min(mdd, v / peak - 1)
    return mdd * 100


def report(df, trades, curve, equity, fixed_pnl, start_bal, trade_usdt, header):
    tdf = pd.DataFrame(trades)
    n = len(tdf)
    wins = int((tdf["return_pct"] > 0).sum()) if n else 0
    bh = (df["close"].iat[-1] / df["open"].iat[0] - 1) * 100
    lines = header + [
        f" Period (UTC)     : {df['time'].iat[0]:%Y-%m-%d %H:%M} -> {df['time'].iat[-1]:%Y-%m-%d %H:%M}  ({len(df)} candles)",
        "-" * 64,
        f" Trades           : {n}",
    ]
    if n:
        avg_w = tdf.loc[tdf.return_pct > 0, "return_pct"].mean() if wins else 0
        avg_l = tdf.loc[tdf.return_pct <= 0, "return_pct"].mean() if n - wins else 0
        gp = tdf.loc[tdf.return_pct > 0, "return_pct"].sum()
        gl = -tdf.loc[tdf.return_pct <= 0, "return_pct"].sum()
        lines += [
            f" Win rate         : {wins / n * 100:.2f}%  ({wins} wins / {n - wins} losses)",
            f" Avg win / loss   : {avg_w:+.3f}% / {avg_l:+.3f}%",
            f" Profit factor    : {gp / gl:.3f}" if gl > 0 else " Profit factor    : n/a (no losses)",
            f" Exit reasons     : {tdf['reason'].value_counts().to_dict()}",
            f" Avg trade length : {tdf['bars'].mean():.1f} candles",
            f" Total return     : {(equity / start_bal - 1) * 100:+.2f}%  (compounding full balance {start_bal:.0f} -> {equity:.2f} USDT)",
            f" Fixed {trade_usdt:.0f} USDT/trade: {fixed_pnl:+.2f} USDT total PnL",
            f" Max drawdown     : {max_drawdown([start_bal] + curve):.2f}%",
        ]
    lines += [f" Buy & hold       : {bh:+.2f}%", "=" * 64]
    return "\n".join(lines), tdf


def main():
    ap = argparse.ArgumentParser(description="Backtest the VWAP + EMA9 strategies")
    ap.add_argument("--config", default=os.path.join(HERE, "config.yaml"))
    ap.add_argument("--strategy", help="video, trend, or a name under strategies: in config.yaml")
    ap.add_argument("--symbol")
    ap.add_argument("--days", type=int)
    ap.add_argument("--start", help="YYYY-MM-DD (UTC)")
    ap.add_argument("--end", help="YYYY-MM-DD (UTC), default now")
    ap.add_argument("--split", help="YYYY-MM-DD: also report before/after this date separately "
                                    "(in-sample / out-of-sample)")
    ap.add_argument("--fee", type=float, help="fee per side in %%, default 0.1")
    ap.add_argument("--short", action="store_true", help="also take short trades")
    ap.add_argument("--anchor", choices=["utc", "new_york"])
    args = ap.parse_args()

    with open(args.config) as fh:
        cfg = yaml.safe_load(fh)
    bt = cfg.get("backtest", {})
    if args.strategy:
        cfg["strategy"] = args.strategy
    if args.anchor:
        cfg["vwap_anchor"] = args.anchor
        cfg["vwap_anchor_custom"] = {}
    p = resolve_params(cfg)
    symbol = args.symbol or bt.get("symbol", "BTC/USDT")
    timeframe = p["timeframe"]
    fee = args.fee if args.fee is not None else float(bt.get("fee_pct", 0.1))
    slip = float(bt.get("slippage_pct", 0.0))
    start_bal = float(bt.get("start_balance_usdt", 1000))
    trade_usdt = float(cfg.get("trade_usdt", 20))

    now = datetime.now(timezone.utc)
    end = datetime.strptime(args.end, "%Y-%m-%d").replace(tzinfo=timezone.utc) if args.end else now
    if args.start:
        start = datetime.strptime(args.start, "%Y-%m-%d").replace(tzinfo=timezone.utc)
    else:
        start = end - timedelta(days=args.days or int(bt.get("days", 60)))
    # warm-up so the first VWAP session and the (trend) EMAs are ready
    warm = max(timedelta(days=1), timedelta(milliseconds=tf_ms(timeframe) * int(p["trend_ema"]) * 3))
    warm_start = start - warm

    print(f"Strategy '{p['name']}': {timeframe}, trend filter {p['trend_filter']}, confirm {p['confirm']}, exit {p['exit_mode']}")
    print(f"Downloading {symbol} {timeframe} candles {warm_start:%Y-%m-%d %H:%M} -> {end:%Y-%m-%d %H:%M} UTC")
    source, rows = download(symbol, timeframe, int(warm_start.timestamp() * 1000), int(end.timestamp() * 1000))
    tz, at = resolve_anchor(cfg)
    full = add_indicators(ohlcv_to_df(rows), int(p["ema_period"]), tz, at, params=p)
    os.makedirs(os.path.join(HERE, "data"), exist_ok=True)

    exit_txt = {"ema": f"first close through EMA{p['ema_period']}, stop {p['stop_loss_pct']}%, "
                       f"TP {p['take_profit_pct'] or 'off'}",
                "atr": f"ATR stop {p['atr_stop_mult']}x, TP {p['atr_tp_r']}R",
                "trail": f"ATR trailing stop {p['trail_atr_mult']}x"}[p["exit_mode"]]
    periods = [("FULL PERIOD", start, end)]
    if args.split:
        sp = datetime.strptime(args.split, "%Y-%m-%d").replace(tzinfo=timezone.utc)
        periods += [("IN-SAMPLE (before split)", start, sp), ("OUT-OF-SAMPLE (after split)", sp, end)]
    safe = symbol.replace("/", "")
    texts = []
    for label, a, b in periods:
        df = full[(full["time"] >= pd.Timestamp(a)) & (full["time"] < pd.Timestamp(b))].reset_index(drop=True)
        trades, curve, equity, fixed_pnl = run_backtest(df, p, fee, slip, args.short, start_bal, trade_usdt)
        header = [
            "=" * 64,
            f" BACKTEST {label}: {symbol} {timeframe}  strategy '{p['name']}'",
            "=" * 64,
            f" Data source      : {source}",
            f" Rules            : EMA{p['ema_period']} x VWAP (anchor {tz} {at}), trend filter "
            f"{p['trend_filter']}{' EMA' + str(p['trend_ema']) if p['trend_filter'] in ('ema', 'both') else ''}, "
            f"confirm {p['confirm']}",
            f" Exit             : {exit_txt}",
            f" Fees / slippage  : {fee}% per side / {slip}% per fill",
            f" Shorts           : {'on' if args.short else 'off'}",
        ]
        text, tdf = report(df, trades, curve, equity, fixed_pnl, start_bal, trade_usdt, header)
        texts.append(text)
        if label == "FULL PERIOD":
            out_csv = os.path.join(HERE, "data", f"backtest_trades_{safe}_{p['name']}.csv")
            tdf.to_csv(out_csv, index=False)
    final = "\n".join(texts) + f"\n Trade list saved : {out_csv}"
    print(final)
    with open(os.path.join(HERE, "data", f"backtest_report_{safe}_{p['name']}.txt"), "w") as fh:
        fh.write(final + "\n")


if __name__ == "__main__":
    main()
