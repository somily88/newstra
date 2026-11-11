# Strategy research: can the video's VWAP + EMA9 idea beat fees?

**Short answer:** the original 3-minute version loses heavily after fees. A slower **1-hour
version with a trend filter** (`strategy: trend`, now the default) did much better, but it is
**not** a reliable money-maker: on BTC it lost money in-sample, made only a small profit
out-of-sample, and did **worse than simply holding** BTC/ETH in the rising out-of-sample period.

## Method (how we tried not to fool ourselves)
* Data: Binance public klines (api.binance.com) via ccxt, BTC/USDT and ETH/USDT, 3m / 5m / 15m / 1h.
  Downloaded 2025-11-10 00:00 → 2026-11-10 23:57 UTC (first day = indicator warm-up).
* **In-sample (IS):** 2025-11-11 00:00 → 2026-07-10 23:59 UTC (8 months, BTC −39.5%, a falling market).
* **Out-of-sample (OOS):** 2026-07-11 00:00 → 2026-11-10 23:59 UTC (4 months, BTC +15.0%, a rising market).
* Parameters chosen on **IS only**; OOS looked at afterwards.
* Fees 0.1% per side always included (0.075% BNB fee also reported). No slippage.
* Signals on candle close, filled at next candle's open. Stop checked before target inside a candle.
* Returns = compounding full balance per trade; drawdown on closed-trade equity.
* Grid (448 combos per coin): timeframe {3m, 5m, 15m, 1h} × longs only / longs+shorts ×
  trend filter {none, close>EMA200, VWAP rising, both} × confirmation {off, close above EMA9 and VWAP}
  × exit {first close through EMA9 + 1.5% stop, ATR stop 1.5/2.5× with 1.5R/3R target, ATR trailing 2×/3×}.
* Selection rule (spot, long only, ≥30 trades on each coin): best **average IS return of BTC and ETH**.

## What we found
* Fees are the main killer. 3m/5m versions trade 500–1000+ times and nearly all lose after fees.
* Every 1h variant beat every 3m variant. No variant was profitable in-sample on BTC (best BTC IS profit factor was 0.74 long-only, 1.08 long+short).
* The winner is robust to small changes (EMA 100/200/300, stop 1%/1.5%/2.5% all gave BTC OOS profit factor 1.09–1.14 and ETH OOS 1.35–1.54).
* Long+short (futures) looked best in the falling IS market but **lost** on BTC out-of-sample → regime dependent, not used.

## Results (0.1% fee per side)

| Strategy | Coin | Period | Trades | Win rate | Return | Profit factor | Max DD | Buy & hold |
|---|---|---|---|---|---|---|---|---|
| ORIGINAL video (3m) | BTC | IS | 1013 | 12.2% | −86.97% | 0.17 | −87.02% | −39.48% |
| ORIGINAL video (3m) | BTC | OOS | 499 | 10.0% | −64.28% | 0.11 | −64.28% | +15.02% |
| ORIGINAL video (3m) | ETH | IS | 1076 | 13.4% | −90.14% | 0.21 | −90.29% | −49.64% |
| ORIGINAL video (3m) | ETH | OOS | 539 | 12.6% | −67.45% | 0.16 | −67.45% | +14.27% |
| **A: trend (winner, default)** | BTC | IS | 94 | 27.7% | −14.30% | 0.65 | −15.22% | −39.48% |
| **A: trend (winner, default)** | BTC | OOS | 45 | 28.9% | **+1.64%** | **1.14** | −8.08% | +15.02% |
| **A: trend (winner, default)** | ETH | IS | 86 | 27.9% | +9.30% | 1.22 | −12.72% | −49.64% |
| **A: trend (winner, default)** | ETH | OOS | 58 | 20.7% | **+10.44%** | **1.41** | −10.99% | +14.27% |
| B: A + VWAP must be rising | BTC | IS | 61 | 27.9% | −15.89% | 0.46 | −16.80% | −39.48% |
| B: A + VWAP must be rising | BTC | OOS | 35 | 31.4% | +7.26% | 1.63 | −4.29% | +15.02% |
| B: A + VWAP must be rising | ETH | IS | 63 | 30.2% | +7.99% | 1.25 | −8.64% | −49.64% |
| B: A + VWAP must be rising | ETH | OOS | 38 | 28.9% | +21.83% | 2.09 | −7.17% | +14.27% |
| C: 1h long+short, EMA200 filter, ATR 2.5× stop, 3R target (futures) | BTC | IS | 84 | 28.6% | +4.72% | 1.08 | −31.55% | −39.48% |
| C: (same) | BTC | OOS | 40 | 25.0% | −9.69% | 0.79 | −16.20% | +15.02% |
| C: (same) | ETH | IS | 99 | 27.3% | +0.72% | 1.06 | −38.61% | −49.64% |
| C: (same) | ETH | OOS | 29 | 31.0% | +20.06% | 1.53 | −13.75% | +14.27% |

Full year (2025-11-11 → 2026-11-10), 0.1% fee: A on BTC 139 trades, −13.10%, PF 0.79, DD −17.36% (hold −30.39%);
A on ETH 144 trades, +20.70%, PF 1.30, DD −12.72% (hold −42.45%).

With 0.075% BNB fee, A on BTC: IS −10.17% (PF 0.74), OOS +3.95% (PF 1.29); ETH IS +14.10% (PF 1.33), OOS +13.69% (PF 1.55).

**Why A and not B?** B looks better out-of-sample, but it was worse in-sample (that is the only data we
were allowed to choose with), and it has fewer trades (35 on BTC OOS). Picking B because of OOS would
be cheating. You can still try it: set `trend_filter: both` in config.yaml.

## Reproduce
```bash
cd research
python dl.py BTC/USDT 1h      # repeat for ETH/USDT and 3m 5m 15m
python grid2.py               # full grid -> grid2.csv
python final.py               # the table above + sensitivity check
# or with the normal backtester:
python ../backtest.py --strategy trend --start 2025-11-11 --end 2026-11-11 --split 2026-07-11
```
