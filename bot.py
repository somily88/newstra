"""
Binance auto-trading bot: VWAP + EMA9 strategies ("trend" on 1h by default, "video" on 3m).

    python bot.py                  # runs forever in the mode set in config.yaml (paper by default)
    python bot.py --iterations 1   # process one closed candle per symbol and stop (quick test)
    python bot.py --config my.yaml

Stop it any time with Ctrl+C. Open positions are remembered in state.json.
"""
from __future__ import annotations

import argparse
import csv
import json
import logging
import os
import signal
import sys
import time
from datetime import datetime, timezone

import ccxt
import yaml
from dotenv import load_dotenv

from strategy import (add_indicators, exit_signal, initial_stops, ohlcv_to_df, resolve_anchor,
                      resolve_params, update_trailing)

HERE = os.path.dirname(os.path.abspath(__file__))
log = logging.getLogger("bot")


# ----------------------------------------------------------------- helpers
def path(p: str) -> str:
    return p if os.path.isabs(p) else os.path.join(HERE, p)


def setup_logging(log_file: str):
    os.makedirs(os.path.dirname(path(log_file)) or ".", exist_ok=True)
    fmt = logging.Formatter("%(asctime)s %(levelname)-7s %(message)s", "%Y-%m-%d %H:%M:%S")
    log.setLevel(logging.INFO)
    for h in (logging.StreamHandler(sys.stdout), logging.FileHandler(path(log_file), encoding="utf-8")):
        h.setFormatter(fmt)
        log.addHandler(h)
    log.addHandler(ERRORS)


def now_ms() -> int:
    return int(time.time() * 1000)


def iso(ms: int) -> str:
    return datetime.fromtimestamp(ms / 1000, timezone.utc).strftime("%Y-%m-%d %H:%M:%S UTC")


class LastErrorHandler(logging.Handler):
    """Remembers the most recent WARNING/ERROR so the dashboard can show it."""

    def __init__(self):
        super().__init__(logging.WARNING)
        self.last = None

    def emit(self, record):
        self.last = {"time_utc": iso(int(record.created * 1000)), "level": record.levelname,
                     "message": record.getMessage()[:500]}


ERRORS = LastErrorHandler()


class State:
    """Positions + last processed candle, saved to disk after every change."""

    def __init__(self, file: str, paper_balance: float):
        self.file = path(file)
        self.data = {"positions": {}, "last_candle": {}, "paper_balance": paper_balance}
        if os.path.exists(self.file):
            with open(self.file) as fh:
                self.data.update(json.load(fh))
            log.info("Loaded state from %s (open positions: %s)", self.file, list(self.data["positions"]) or "none")

    def save(self):
        tmp = self.file + ".tmp"
        with open(tmp, "w") as fh:
            json.dump(self.data, fh, indent=2)
        os.replace(tmp, self.file)  # atomic: never leaves a half-written file

    @property
    def positions(self) -> dict:
        return self.data["positions"]


class TradeLog:
    FIELDS = ["time_utc", "mode", "symbol", "side", "action", "price", "amount",
              "notional_usdt", "fee_usdt", "pnl_usdt", "pnl_pct", "reason", "order_id"]

    def __init__(self, file: str):
        self.file = path(file)
        os.makedirs(os.path.dirname(self.file) or ".", exist_ok=True)
        if not os.path.exists(self.file):
            with open(self.file, "w", newline="") as fh:
                csv.writer(fh).writerow(self.FIELDS)

    def write(self, **row):
        row.setdefault("time_utc", iso(now_ms()))
        with open(self.file, "a", newline="") as fh:
            csv.DictWriter(fh, self.FIELDS).writerow({k: row.get(k, "") for k in self.FIELDS})


# ----------------------------------------------------------------- the bot
class Bot:
    def __init__(self, cfg: dict):
        self.cfg = cfg
        self.mode = str(cfg.get("mode", "paper")).lower()
        self.market_type = str(cfg.get("market_type", "spot")).lower()
        self.symbols = list(cfg.get("symbols") or ["BTC/USDT"])
        if self.market_type == "futures":  # ccxt names USDT-M perpetuals like "BTC/USDT:USDT"
            self.symbols = [s if ":" in s else f"{s}:{s.split('/')[1]}" for s in self.symbols]
        self.p = resolve_params(cfg)
        self.tf = self.p["timeframe"]
        self.tf_ms = ccxt.Exchange.parse_timeframe(self.tf) * 1000
        self.ema_period = int(self.p["ema_period"])
        self.anchor_tz, self.anchor_time = resolve_anchor(cfg)
        self.allow_short = bool(cfg.get("allow_short", False))
        self.trade_usdt = float(cfg.get("trade_usdt", 20))
        self.exchange_stop = bool(cfg.get("exchange_stop_order", False)) and self.mode != "paper"
        self.poll = max(1, int(cfg.get("poll_seconds", 5)))
        self.max_retries = int(cfg.get("max_retries", 5))
        self.paper_fee = float(cfg.get("paper_fee_pct", 0.1)) / 100
        self.paper_slip = float(cfg.get("paper_slippage_pct", 0.02)) / 100

        self._safety_checks()
        self.ex = self._make_exchange()
        self.state = State(cfg.get("state_file", "state.json") if self.mode == "paper"
                           else cfg.get("state_file", "state.json").replace(".json", f"_{self.mode}.json"),
                           float(cfg.get("paper_start_balance_usdt", 1000)))
        self.trades = TradeLog(cfg.get("trade_log_csv", "logs/trades.csv"))
        self.status_file = path(os.getenv("STATUS_FILE") or
                                os.path.join(os.path.dirname(path(cfg.get("state_file", "state.json"))), "status.json"))
        self.started = iso(now_ms())
        self.candle_info = {}   # latest closed-candle values per symbol (for the dashboard)
        self.last_prices = {}
        self._last_status_write = 0
        self.exchange_balance = None
        self._last_bal_fetch = 0

    # ---------------- setup
    def _safety_checks(self):
        if self.mode not in ("paper", "testnet", "live"):
            raise SystemExit(f"mode must be paper, testnet or live (got {self.mode!r})")
        if self.market_type not in ("spot", "futures"):
            raise SystemExit("market_type must be spot or futures")
        if self.allow_short and self.market_type != "futures":
            log.warning("allow_short only works with market_type: futures -> shorts DISABLED")
            self.allow_short = False
        if self.mode == "live" and os.getenv("LIVE_TRADING", "").strip().lower() != "yes":
            raise SystemExit("REFUSING TO TRADE REAL MONEY: config says mode: live but LIVE_TRADING=yes "
                             "is not set in your .env / environment.")
        if self.mode != "paper" and not (os.getenv("BINANCE_API_KEY") and os.getenv("BINANCE_API_SECRET")):
            raise SystemExit(f"mode {self.mode} needs BINANCE_API_KEY and BINANCE_API_SECRET in .env")

    def _make_exchange(self):
        opts = {"enableRateLimit": True, "options": {"adjustForTimeDifference": True}}
        if self.mode != "paper":
            opts["apiKey"] = os.getenv("BINANCE_API_KEY")
            opts["secret"] = os.getenv("BINANCE_API_SECRET")
        cls = ccxt.binanceusdm if self.market_type == "futures" else ccxt.binance
        ex = cls(opts)
        if self.mode == "testnet":
            # ccxt no longer supports the old futures testnet -> futures always use Binance Demo Trading
            if self.cfg.get("use_demo_trading") or self.market_type == "futures":
                ex.enable_demo_trading(True)
            else:
                ex.set_sandbox_mode(True)
        self.retry(ex.load_markets)
        for s in self.symbols:
            if s not in ex.markets:
                raise SystemExit(f"Symbol {s} not found on Binance {self.market_type}")
        if self.market_type == "futures" and self.mode != "paper":
            for s in self.symbols:
                try:
                    ex.set_leverage(int(self.cfg.get("futures_leverage", 1)), s)
                except Exception as e:
                    log.warning("Could not set leverage on %s: %s", s, e)
        return ex

    # ---------------- network safety
    def retry(self, fn, *a, **kw):
        for attempt in range(1, self.max_retries + 1):
            try:
                return fn(*a, **kw)
            except (ccxt.NetworkError, ccxt.ExchangeNotAvailable, ccxt.RequestTimeout, ccxt.DDoSProtection) as e:
                wait = min(60, 2 ** attempt)
                log.warning("Network problem (%s: %s). Retry %d/%d in %ds",
                            type(e).__name__, str(e)[:120], attempt, self.max_retries, wait)
                time.sleep(wait)
        return fn(*a, **kw)  # last try: let the error go up

    # ---------------- market data
    def closed_candles(self, symbol: str):
        raw = self.retry(self.ex.fetch_ohlcv, symbol, self.tf, None, int(self.cfg.get("candles_to_fetch", 1000)))
        cutoff = now_ms()
        raw = [r for r in raw if r[0] + self.tf_ms <= cutoff]  # drop the still-open candle
        df = ohlcv_to_df(raw)
        return add_indicators(df, self.ema_period, self.anchor_tz, self.anchor_time, params=self.p)

    def price(self, symbol: str) -> float:
        t = self.retry(self.ex.fetch_ticker, symbol)
        return float(t.get("last") or t.get("close"))

    # ---------------- order helpers
    def _amount_for(self, symbol: str, price: float):
        m = self.ex.market(symbol)
        amount = float(self.ex.amount_to_precision(symbol, self.trade_usdt / price))
        min_amt = (m.get("limits", {}).get("amount", {}) or {}).get("min") or 0
        min_cost = (m.get("limits", {}).get("cost", {}) or {}).get("min") or 0
        if amount <= 0 or amount < min_amt:
            log.warning("%s: trade_usdt=%.2f too small (min amount %s). Skipping.", symbol, self.trade_usdt, min_amt)
            return None
        if min_cost and amount * price < min_cost:
            log.warning("%s: order value %.2f < Binance minimum %.2f USDT. Raise trade_usdt.",
                        symbol, amount * price, min_cost)
            return None
        return amount

    def _market(self, symbol, side, amount, reduce_only=False):
        """Returns (avg_price, filled_amount, fee_usdt, order_id)."""
        if self.mode == "paper":
            p = self.price(symbol)
            p = p * (1 + self.paper_slip) if side == "buy" else p * (1 - self.paper_slip)
            return p, amount, p * amount * self.paper_fee, "paper"
        params = {"reduceOnly": True} if (reduce_only and self.market_type == "futures") else {}
        o = self.ex.create_order(symbol, "market", side, amount, None, params)  # no auto-retry: avoid double orders
        try:
            o = self.retry(self.ex.fetch_order, o["id"], symbol)
        except Exception:
            pass
        avg = float(o.get("average") or o.get("price") or self.price(symbol))
        filled = float(o.get("filled") or amount)
        fee = 0.0
        for f in o.get("fees") or ([o["fee"]] if o.get("fee") else []):
            if f and f.get("cost"):
                fee += float(f["cost"]) * (avg if f.get("currency") == self.ex.market(symbol)["base"] else 1)
        return avg, filled, fee, o.get("id")

    def _sellable_amount(self, symbol, amount):
        """Spot: Binance may take the fee from the coin you bought, so sell what you really hold."""
        if self.mode == "paper" or self.market_type != "spot":
            return amount
        base = self.ex.market(symbol)["base"]
        free = float((self.retry(self.ex.fetch_balance).get(base) or {}).get("free") or 0)
        amt = float(self.ex.amount_to_precision(symbol, min(amount, free)))
        return amt

    def _place_exchange_stop(self, symbol, pos):
        if not self.exchange_stop or not pos.get("stop"):
            return
        side = "sell" if pos["side"] == "long" else "buy"
        stop = float(self.ex.price_to_precision(symbol, pos["stop"]))
        try:
            if self.market_type == "futures":
                o = self.ex.create_order(symbol, "STOP_MARKET", side, pos["amount"], None,
                                         {"stopPrice": stop, "reduceOnly": True})
            else:
                limit = float(self.ex.price_to_precision(symbol, stop * 0.995))  # limit a bit below trigger
                amt = self._sellable_amount(symbol, pos["amount"])
                o = self.ex.create_order(symbol, "STOP_LOSS_LIMIT", side, amt, limit,
                                         {"stopPrice": stop, "timeInForce": "GTC"})
            pos["stop_order_id"] = o["id"]
            log.info("%s: exchange stop order placed at %s (id %s)", symbol, stop, o["id"])
        except Exception as e:
            log.error("%s: could NOT place exchange stop (%s). Bot will still watch the stop itself.", symbol, e)

    def _cancel_exchange_stop(self, symbol, pos):
        oid = pos.get("stop_order_id")
        if not oid:
            return
        try:
            self.ex.cancel_order(oid, symbol)
        except ccxt.OrderNotFound:
            pass
        except Exception as e:
            log.warning("%s: cancel stop order %s failed: %s", symbol, oid, e)

    # ---------------- entries / exits
    def open_position(self, symbol: str, side: str, ref_price: float, atr: float):
        if symbol in self.state.positions:
            return  # one position per symbol
        amount = self._amount_for(symbol, ref_price)
        if amount is None:
            return
        if self.mode == "paper" and self.state.data["paper_balance"] < self.trade_usdt:
            log.warning("Paper balance too low (%.2f). Skipping.", self.state.data["paper_balance"])
            return
        try:
            price, filled, fee, oid = self._market(symbol, "buy" if side == "long" else "sell", amount)
        except ccxt.InsufficientFunds as e:
            log.error("%s: not enough balance to open trade: %s", symbol, e)
            return
        except Exception as e:
            log.error("%s: entry order failed: %s", symbol, e)
            return
        stop, tp = initial_stops(side, price, atr, self.p)
        pos = {"side": side, "entry": price, "amount": filled, "entry_fee": fee,
               "opened": iso(now_ms()), "stop": stop, "tp": tp, "order_id": oid}
        if self.mode == "paper":
            self.state.data["paper_balance"] -= price * filled + fee
        self.state.positions[symbol] = pos
        self.state.save()
        self._place_exchange_stop(symbol, pos)
        self.state.save()
        log.info(">>> OPEN %s %s %.8g @ %.2f  (stop %s, tp %s)", side.upper(), symbol, filled, price,
                 f"{stop:.2f}" if stop else "off", f"{tp:.2f}" if tp else "off")
        self.trades.write(mode=self.mode, symbol=symbol, side=side, action="OPEN", price=round(price, 8),
                          amount=filled, notional_usdt=round(price * filled, 4), fee_usdt=round(fee, 6),
                          reason="ema9_cross_vwap", order_id=oid)

    def close_position(self, symbol: str, reason: str, already_filled_price: float | None = None):
        pos = self.state.positions.get(symbol)
        if not pos:
            return
        side = pos["side"]
        if already_filled_price is not None:  # exchange stop already executed
            price, filled, fee, oid = already_filled_price, pos["amount"], 0.0, pos.get("stop_order_id")
        else:
            self._cancel_exchange_stop(symbol, pos)
            amount = self._sellable_amount(symbol, pos["amount"])
            if amount <= 0:
                log.error("%s: nothing left to sell (balance 0). Removing position from state.", symbol)
                self.state.positions.pop(symbol)
                self.state.save()
                return
            try:
                price, filled, fee, oid = self._market(symbol, "sell" if side == "long" else "buy",
                                                       amount, reduce_only=True)
            except Exception as e:
                log.error("%s: EXIT ORDER FAILED (%s). Will try again next check.", symbol, e)
                return
        gross = (price - pos["entry"]) * filled if side == "long" else (pos["entry"] - price) * filled
        pnl = gross - fee - pos.get("entry_fee", 0)
        pnl_pct = pnl / (pos["entry"] * filled) * 100 if filled else 0
        if self.mode == "paper":
            self.state.data["paper_balance"] += pos["entry"] * filled + gross - fee
        self.state.positions.pop(symbol)
        self.state.save()
        log.info("<<< CLOSE %s %s @ %.2f  reason=%s  PnL %+.4f USDT (%+.2f%%)%s", side.upper(), symbol, price,
                 reason, pnl, pnl_pct,
                 f"  paper balance {self.state.data['paper_balance']:.2f}" if self.mode == "paper" else "")
        self.trades.write(mode=self.mode, symbol=symbol, side=side, action="CLOSE", price=round(price, 8),
                          amount=filled, notional_usdt=round(price * filled, 4), fee_usdt=round(fee, 6),
                          pnl_usdt=round(pnl, 6), pnl_pct=round(pnl_pct, 4), reason=reason, order_id=oid)

    # ---------------- per-candle logic
    def on_candle(self, symbol: str) -> bool:
        df = self.closed_candles(symbol)
        if len(df) < self.ema_period * 3 + 2:
            log.warning("%s: not enough candles yet (%d)", symbol, len(df))
            return False
        last = df.iloc[-1]
        ts = int(last["ts"])
        self.candle_info[symbol] = {
            "candle_open_utc": iso(ts), "ts": ts, "close": float(last["close"]), "ema": float(last["ema"]),
            "vwap": float(last["vwap"]), "ema_trend": float(last["ema_trend"]), "atr": float(last["atr"]),
            "above_trend": bool(last["close"] > last["ema_trend"]), "cross_up": bool(last["cross_up"]),
            "cross_down": bool(last["cross_down"]), "long_signal": bool(last["long_signal"]),
            "short_signal": bool(last["short_signal"]) if self.allow_short else False,
        }
        self.last_prices[symbol] = float(last["close"])
        if self.state.data["last_candle"].get(symbol) == ts:
            return False  # already handled this candle
        pos = self.state.positions.get(symbol)
        log.info("%s candle %s close=%.2f EMA%d=%.2f VWAP=%.2f EMA%d=%.2f  cross_up=%s  buy_signal=%s%s  position=%s",
                 symbol, iso(ts), last["close"], self.ema_period, last["ema"], last["vwap"],
                 int(self.p["trend_ema"]), last["ema_trend"], bool(last["cross_up"]), bool(last["long_signal"]),
                 f"  sell_short_signal={bool(last['short_signal'])}" if self.allow_short else "",
                 pos["side"] if pos else "none")

        if pos:
            opened_ms = int(datetime.strptime(pos["opened"], "%Y-%m-%d %H:%M:%S UTC")
                            .replace(tzinfo=timezone.utc).timestamp() * 1000)
            # only candles that started after we entered count as "after entry"
            after_entry = ts >= opened_ms - self.tf_ms
            if after_entry and self.p["exit_mode"] == "ema" and exit_signal(pos["side"], last["close"], last["ema"]):
                self.close_position(symbol, "close_through_ema")
            elif after_entry and self.p["exit_mode"] == "trail":
                new_stop = update_trailing(pos["side"], pos, float(last["close"]), float(last["atr"]), self.p)
                if new_stop != pos["stop"]:
                    log.info("%s: trailing stop moved %.2f -> %.2f", symbol, pos["stop"], new_stop)
                    pos["stop"] = new_stop
                    if pos.get("stop_order_id"):
                        self._cancel_exchange_stop(symbol, pos)
                        pos["stop_order_id"] = None
                        self._place_exchange_stop(symbol, pos)
                    self.state.save()
            pos = self.state.positions.get(symbol)
        cooldown_until = self.state.data.setdefault("cooldown_until", {}).get(symbol, 0)
        if not pos and ts >= cooldown_until:
            atr = float(last["atr"])
            if last["long_signal"]:
                self.open_position(symbol, "long", float(last["close"]), atr)
            elif self.allow_short and last["short_signal"]:
                self.open_position(symbol, "short", float(last["close"]), atr)

        self.state.data["last_candle"][symbol] = ts
        self.state.save()
        return True

    # ---------------- stop loss / take profit watcher
    def check_stops(self):
        for symbol, pos in list(self.state.positions.items()):
            try:
                if pos.get("stop_order_id") and self.mode != "paper":
                    o = self.retry(self.ex.fetch_order, pos["stop_order_id"], symbol)
                    if o.get("status") == "closed":
                        self.close_position(symbol, "stop_loss_exchange",
                                            already_filled_price=float(o.get("average") or o.get("price")))
                        continue
                p = self.price(symbol)
                self.last_prices[symbol] = p
                st, tp = pos.get("stop"), pos.get("tp")
                if pos["side"] == "long":
                    if st and p <= st:
                        self.close_position(symbol, "stop_loss")
                        self._start_cooldown(symbol)
                    elif tp and p >= tp:
                        self.close_position(symbol, "take_profit")
                else:
                    if st and p >= st:
                        self.close_position(symbol, "stop_loss")
                        self._start_cooldown(symbol)
                    elif tp and p <= tp:
                        self.close_position(symbol, "take_profit")
            except Exception as e:
                log.error("%s: stop check failed: %s", symbol, e)

    def _start_cooldown(self, symbol):
        bars = int(self.p.get("cooldown_bars") or 0)
        if bars:
            self.state.data.setdefault("cooldown_until", {})[symbol] = (now_ms() // self.tf_ms + bars) * self.tf_ms
            self.state.save()

    def reconcile(self):
        """Warn if state.json says we hold something the exchange account does not show."""
        if self.mode == "paper" or self.market_type != "spot":
            return
        try:
            bal = self.retry(self.ex.fetch_balance)
            for s, pos in self.state.positions.items():
                base = self.ex.market(s)["base"]
                total = float((bal.get(base) or {}).get("total") or 0)
                if total < pos["amount"] * 0.5:
                    log.warning("%s: state says we hold %s %s but account shows %s. Check manually!",
                                s, pos["amount"], base, total)
        except Exception as e:
            log.warning("Could not reconcile balances: %s", e)

    def refresh_exchange_balance(self, force: bool = False):
        """Read the account balance (testnet/live) for the dashboard, at most once a minute."""
        if self.mode == "paper" or (not force and time.time() - self._last_bal_fetch < 60):
            return
        self._last_bal_fetch = time.time()
        try:
            bal = self.ex.fetch_balance()
            assets = ["USDT"] + sorted({self.ex.market(s)["base"] for s in self.symbols})
            self.exchange_balance = {a: {"free": float((bal.get(a) or {}).get("free") or 0),
                                         "total": float((bal.get(a) or {}).get("total") or 0)} for a in assets}
        except Exception as e:
            log.warning("Could not read exchange balance: %s", e)

    # ---------------- heartbeat for the dashboard (read-only file, no keys inside)
    def write_status(self, phase: str, next_candle_ms: int | None = None):
        self.refresh_exchange_balance()
        positions = {}
        for s, pos in self.state.positions.items():
            price = self.last_prices.get(s)
            upnl = upct = None
            if price:
                d = 1 if pos["side"] == "long" else -1
                upnl = (price - pos["entry"]) * pos["amount"] * d
                upct = (price / pos["entry"] - 1) * 100 * d
            positions[s] = {**{k: pos.get(k) for k in ("side", "entry", "amount", "opened", "stop", "tp")},
                            "last_price": price, "unrealized_pnl_usdt": upnl, "unrealized_pnl_pct": upct}
        status = {
            "phase": phase, "heartbeat_utc": iso(now_ms()), "heartbeat_ms": now_ms(), "started_utc": self.started,
            "pid": os.getpid(), "mode": self.mode, "market_type": self.market_type, "strategy": self.p["name"],
            "strategy_params": {k: v for k, v in self.p.items() if k != "name"},
            "symbols": self.symbols, "timeframe": self.tf, "trade_usdt": self.trade_usdt,
            "allow_short": self.allow_short, "vwap_anchor": f"{self.anchor_tz} {self.anchor_time}",
            "next_candle_close_utc": iso(next_candle_ms) if next_candle_ms else None,
            "paper_balance_usdt": self.state.data["paper_balance"] if self.mode == "paper" else None,
            "exchange_balance": self.exchange_balance,
            "positions": positions, "latest_candles": self.candle_info, "last_error": ERRORS.last,
            "files": {"trades_csv": self.trades.file, "log_file": path(self.cfg.get("log_file", "logs/bot.log")),
                      "state_file": self.state.file},
        }
        try:
            tmp = self.status_file + ".tmp"
            with open(tmp, "w") as fh:
                json.dump(status, fh, indent=2, default=str)
            os.replace(tmp, self.status_file)
            self._last_status_write = time.time()
        except Exception as e:
            log.debug("could not write status: %s", e)

    # ---------------- main loop
    def run(self, iterations: int | None = None):
        banner = {"paper": "PAPER (fake money, real prices)", "testnet": "TESTNET (test money)",
                  "live": "!!! LIVE - REAL MONEY !!!"}[self.mode]
        log.info("=" * 70)
        log.info("Mode: %s | market: %s | symbols: %s | tf: %s", banner, self.market_type, self.symbols, self.tf)
        p = self.p
        exit_txt = {"ema": f"close through EMA{self.ema_period} / stop {p['stop_loss_pct']}% / "
                           f"TP {str(p['take_profit_pct']) + '%' if p['take_profit_pct'] else 'off'}",
                    "atr": f"ATR stop {p['atr_stop_mult']}x / TP {p['atr_tp_r']}R",
                    "trail": f"ATR trailing stop {p['trail_atr_mult']}x"}[p["exit_mode"]]
        log.info("Strategy '%s': EMA%d x VWAP (anchor %s %s) | trend filter %s (EMA%s) | confirm %s",
                 p["name"], self.ema_period, self.anchor_tz, self.anchor_time, p["trend_filter"],
                 p["trend_ema"], p["confirm"])
        log.info("Exit: %s | %.2f USDT/trade | shorts %s", exit_txt, self.trade_usdt, self.allow_short)
        if self.mode == "paper":
            log.info("Paper balance: %.2f USDT", self.state.data["paper_balance"])
        log.info("=" * 70)
        self.reconcile()

        done = 0
        while True:
            for s in self.symbols:
                try:
                    self.on_candle(s)
                except Exception as e:
                    log.exception("%s: error while processing candle: %s", s, e)
            done += 1
            if iterations is not None and done >= iterations:
                log.info("Finished %d iteration(s). Exiting.", done)
                self.write_status("stopped")
                return
            # wait for the next candle close, checking stop/take-profit meanwhile
            next_close = (now_ms() // self.tf_ms + 1) * self.tf_ms + 2000  # +2s so Binance has the candle
            log.info("Waiting for next candle close at %s ...", iso(next_close - 2000))
            self.write_status("waiting", next_close - 2000)
            while now_ms() < next_close:
                if self.state.positions:
                    self.check_stops()
                if time.time() - self._last_status_write >= 10:
                    self.write_status("waiting", next_close - 2000)
                time.sleep(min(self.poll, max(0.5, (next_close - now_ms()) / 1000)))


def main():
    ap = argparse.ArgumentParser(description="Binance VWAP + EMA9 trading bot")
    ap.add_argument("--config", default=os.path.join(HERE, "config.yaml"))
    ap.add_argument("--iterations", type=int, help="stop after N candle checks (for testing)")
    args = ap.parse_args()

    load_dotenv(os.path.join(HERE, ".env"))
    with open(args.config) as fh:
        cfg = yaml.safe_load(fh)
    # Environment variables can move the files (used by Docker to keep them in ./data)
    for key, env in (("state_file", "STATE_FILE"), ("log_file", "LOG_FILE"), ("trade_log_csv", "TRADE_LOG_CSV")):
        if os.getenv(env):
            cfg[key] = os.getenv(env)
    setup_logging(cfg.get("log_file", "logs/bot.log"))

    # "docker compose down/stop" sends SIGTERM: treat it like Ctrl+C so we exit cleanly.
    def _on_sigterm(signum, frame):
        raise KeyboardInterrupt
    signal.signal(signal.SIGTERM, _on_sigterm)

    bot = None
    try:
        bot = Bot(cfg)
        bot.run(args.iterations)
    except KeyboardInterrupt:
        log.info("Stop signal received. Saving state and exiting.")
    finally:
        if bot is not None:
            try:
                bot.state.save()
                bot.write_status("stopped")
                log.info("State saved to %s (open positions: %s)", bot.state.file,
                         list(bot.state.positions) or "none")
            except Exception as e:
                log.error("Could not save state on exit: %s", e)


if __name__ == "__main__":
    main()
