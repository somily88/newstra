# Binance VWAP + EMA 9 Trading Bot

A simple auto-trading bot for **Binance** based on the strategy from your video.

It has two strategies (pick one in `config.yaml` with `strategy:`):

* **`trend` (default)** – the video idea, made slower and safer after testing 12 months of data:
  1-hour candles, buy when EMA 9 crosses above VWAP **only if** price is above the EMA 200
  (up-trend) and the candle closes above both EMA 9 and VWAP. Sell on the first 1-hour candle that
  closes below EMA 9, or at a 1.5% stop loss. See **RESEARCH.md** for the test results.
* **`video`** – exactly the video's rules (below). It lost a lot of money after fees in our tests.

The original `video` rules:

| Rule | What the bot does |
|---|---|
| Timeframe | 3-minute candles |
| Indicators | **VWAP** (resets every day at 00:00 UTC; can be set to 09:30 New York) and **EMA 9** |
| Buy (long) | When EMA 9 **crosses above** VWAP and the candle closes |
| Sell | The first candle that **closes below EMA 9** after you bought |
| Stop loss | Sell if price falls **1.5%** below your buy price (you can change this) |
| Take profit | Off by default (you can turn it on) |
| Short selling | Off by default. Only possible on futures (`allow_short: true`) |
| Size | **20 USDT** per trade, max one trade per coin at a time |

> The video used a -25% / -30% stop because it traded **options**. Crypto spot moves much less,
> so the bot uses a 1.5% price stop instead.

**It starts in PAPER mode**: fake money, real live prices. No API keys, no risk.

---

## 1. Install (one time)

You need Python 3.10 or newer (https://www.python.org/downloads/).

```bash
cd binance-vwap-bot
pip install -r requirements.txt
```

## 2. Run it

```bash
python bot.py                  # paper mode, runs until you press Ctrl+C
python bot.py --iterations 1   # quick test: check the latest candle once, then stop
python backtest.py             # test the strategy on the last 60 days of BTC/USDT
```

What you will see: after every candle (every 1 hour for `trend`, every 3 minutes for `video`) a line with the price, EMA 9, VWAP, and whether a cross happened.
When it trades you see `>>> OPEN` and `<<< CLOSE` lines.

Files the bot creates:

* `logs/bot.log`  – everything the bot printed
* `logs/trades.csv` – every buy and sell, with profit/loss (open it in Excel)
* `state.json` – open positions. If the bot restarts it remembers them, so it will **not buy twice**.
  (Testnet uses `state_testnet.json`, live uses `state_live.json`.)
* `status.json` – the bot's heartbeat, read by the dashboard

All settings are in **`config.yaml`** (open it with any text editor; every line is explained).

## 2b. Run with Docker (easiest, works the same on Windows / Mac / Linux)

Docker runs the bot in its own little box, so you don't need to install Python.

1. Install **Docker Desktop**: https://www.docker.com/products/docker-desktop/ — open it once and
   wait until it says "Engine running". (Linux: install Docker Engine + the compose plugin.)
2. Open a terminal in the `binance-vwap-bot` folder.
3. Create your settings file for secrets (paper mode works even if you leave it empty):
   ```bash
   cp .env.example .env        # Windows PowerShell: copy .env.example .env
   ```
4. Start the bot and the dashboard (first time takes a minute to build):
   ```bash
   docker compose up -d --build
   ```
5. Watch what it is doing (Ctrl+C only stops watching, not the bot):
   ```bash
   docker compose logs -f bot
   ```
6. Stop the bot:
   ```bash
   docker compose down
   ```

The bot starts again by itself if your computer restarts (as long as Docker Desktop is running),
until you run `docker compose down`.

Then open the dashboard: **http://localhost:8765** (see section 2c).

**Where are my files?** In the `data` folder next to `config.yaml`:
`data/state.json` (open positions, so a restart never buys twice), `data/trades.csv`
(all trades), `data/logs/bot.log`, `data/status.json` (heartbeat for the dashboard). Log times are in UTC.

**Change settings:** edit `config.yaml`, then restart: `docker compose restart bot`.
(The file is mounted read-only into the container, no rebuild needed.)

**Run a backtest in Docker:**
```bash
docker compose run --rm backtest                       # default: BTC/USDT, last 60 days
docker compose run --rm backtest --strategy video --days 30
docker compose run --rm backtest --symbol ETH/USDT --start 2025-11-11 --end 2026-11-11 --split 2026-07-11
```
Results are saved in `data/`.

**Testnet or live with Docker:** exactly the same as without Docker:
* testnet: `mode: testnet` in `config.yaml` + testnet keys in `.env`
* live: `mode: live` in `config.yaml` **and** `LIVE_TRADING=yes` + real keys in `.env`

After changing `.env` run `docker compose up -d` again (a plain restart does not reload `.env`).
If live is not unlocked, the bot refuses to start and `docker compose logs bot` shows
"REFUSING TO TRADE REAL MONEY" (Docker will keep retrying — run `docker compose down`).

Your `.env` keys are **never** copied into the Docker image; they are only passed in when it runs.

## 2c. Dashboard (see what the bot is doing in your browser)

A simple web page that shows:
* **Status** – PAPER / TESTNET / LIVE badge, strategy, coins, timeframe, "Running" + last heartbeat, last error
* **Open position** – entry price, stop loss, profit/loss right now
* **Latest candle** – close, EMA 9, VWAP, EMA 200, and whether there is a BUY signal
* **Price chart** – candles with EMA 9, VWAP, EMA 200 and ▲ buy / ▼ sell arrows from your trades
* **Trade history**, **results** (win rate, total P&L, paper balance), **latest log lines**, **backtest report**

It refreshes every 12 seconds. The page is in **Arabic by default** (right-to-left); the
**English / العربية** button in the top corner switches language (your choice is remembered).
The **الوقت: UTC / Times: UTC** button switches to your computer's local time.

**The dashboard is read-only.** It has no API keys and cannot place, change or cancel orders.
It only reads the files the bot writes (`status.json`, trades CSV, log) and public Binance prices for the chart.

### With Docker
`docker compose up -d --build` starts the bot **and** the dashboard. Open: **http://localhost:8765**

It only listens on your own computer (127.0.0.1), so other people on your network can't open it.

### Without Docker
Open a second terminal (the bot keeps running in the first one):
```bash
python dashboard.py
```
Then open **http://localhost:8765**.

### Optional password
Put this line in `.env` (Docker) or set it in the terminal before `python dashboard.py`:
```
DASHBOARD_PASSWORD=choose-a-password
```
The browser will ask for a username and password: type anything as the username, and your password.
(Docker: run `docker compose up -d` again after changing `.env`.)

### Change the port / Windows "port not allowed" error
The dashboard uses port **8765** by default. To use another one, set it in `.env`, for example
`DASHBOARD_PORT=9123`, run `docker compose up -d` again and open `http://localhost:9123`.
Without Docker: `DASHBOARD_PORT=9123 python dashboard.py` (Windows PowerShell:
`$env:DASHBOARD_PORT=9123; python dashboard.py`).

**Windows:** if Docker says something like *"ports are not available ... An attempt was made to access a
socket in a way forbidden by its access permissions"* (or "bind: ... refused"), Windows has reserved
that port. See which ports are blocked:
```
netsh interface ipv4 show excludedportrange protocol=tcp
```
Pick a port that is **not** inside any listed range (e.g. 8765 or 9123), put it in `DASHBOARD_PORT`,
and start again.

If the page says "Bot has not run yet", start the bot first. "Not running" means the bot's heartbeat
is older than 90 seconds or it was stopped.

## 3. Backtest (check the strategy on past data)

```bash
python backtest.py                                  # BTC/USDT, last 60 days, 0.1% fee, strategy from config
python backtest.py --strategy video                 # the original video rules
python backtest.py --start 2025-11-11 --end 2026-11-11 --split 2026-07-11   # report before/after a date
python backtest.py --symbol ETH/USDT --days 30
python backtest.py --start 2026-09-01 --end 2026-10-31
python backtest.py --anchor new_york                # VWAP reset at 09:30 New York
python backtest.py --short                          # also test short trades
python backtest.py --fee 0.075                      # fee if you pay fees with BNB
```

It prints number of trades, win rate, total return, max drawdown, and saves the trade list in `data/`.

## 4. Get Binance API keys (only for testnet or live)

**Paper mode does not need keys.**

### Testnet (practice money)
* Spot: go to https://testnet.binance.vision, log in with GitHub, click **Generate HMAC_SHA256 Key**.
* Futures: use Binance **Demo Trading** (https://demo.binance.com) and create an API key there.

### Real account (live)
1. Log in to Binance → profile icon → **API Management** → **Create API** → "System generated".
2. Permissions — VERY IMPORTANT:
   * ✅ **Enable Reading**
   * ✅ **Enable Spot & Margin Trading** (or Futures, only if you use futures)
   * ❌ **NEVER enable Withdrawals**
3. Choose **Restrict access to trusted IPs only** and enter the IP of the computer running the bot
   (search "what is my IP" in Google).
4. Copy the key and secret.

### Put the keys in the bot
```bash
cp .env.example .env      # Windows: copy .env.example .env
```
Open `.env` and paste:
```
BINANCE_API_KEY=your_key
BINANCE_API_SECRET=your_secret
```
Never share `.env` with anyone. Never put keys inside `config.yaml` or the code.

## 5. Modes

| Mode | Money | How to turn on |
|---|---|---|
| `paper` (default) | fake | nothing to do |
| `testnet` | test money | `mode: testnet` in config.yaml + testnet keys in `.env` |
| `live` | **REAL** | `mode: live` in config.yaml **AND** `LIVE_TRADING=yes` in `.env` + real keys |

Live mode refuses to start unless **both** switches are set. Start live with a small `trade_usdt`
(spot minimum is about 5 USDT per order; futures minimum is about 50–100 USDT).

### Futures (optional)
In `config.yaml`: `market_type: futures`, `futures_leverage: 1`, optionally `allow_short: true`.
Remember futures can lose more than you expect. Keep leverage at 1.

### Stop loss safety
The bot checks the stop loss every 5 seconds while it runs. If your computer turns off, it cannot
check. For extra safety in testnet/live set `exchange_stop_order: true` so a real stop order is also
placed on Binance.

## 6. Settings you will most likely change (`config.yaml`)

* `symbols` – e.g. `[BTC/USDT, ETH/USDT, SOL/USDT]`
* `trade_usdt` – how much per trade
* `stop_loss_pct` – e.g. `1.5`
* `take_profit_pct` – e.g. `3` (0 = off)
* `vwap_anchor` – `utc` or `new_york`
* `strategy` – `trend` (default) or `video`. Under `strategies:` you can change each one, e.g.
  `trend_filter: both` (also require VWAP to be rising), `exit_mode: atr` or `trail`.

## ⚠️ Risk warning (please read)

* This is **not financial advice**. Trading crypto is risky; you can lose money, including all of it.
* **Past results do not guarantee future results.** Run the backtest yourself first.
  The original 3-minute `video` strategy **lost heavily** after fees (−64% to −90% in our tests).
  The default `trend` strategy did better but is weak: on BTC/USDT it lost −14.3% in the first
  8 months tested and made only +1.6% in the last 4 months, while just holding BTC made +15%
  in those 4 months (details in RESEARCH.md).
* Run in **paper** mode for days, then **testnet**, and only then try **live** with a small amount.
* Bugs, internet problems, Binance outages, or sudden price moves can cause losses or missed exits.
* Binance is not available in every country. Follow your local laws.
* You are fully responsible for any trades the bot makes.
