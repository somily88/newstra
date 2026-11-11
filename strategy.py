"""
Strategy logic shared by the live bot and the backtester.

Core idea (from the video): EMA 9 crossing the session-anchored VWAP.

Two ready-made presets (choose with `strategy:` in config.yaml):

  video  - exactly the video: 3m candles, buy when EMA9 crosses above VWAP,
           sell on the first close below EMA9 (or 1.5% stop).
  trend  - (DEFAULT, tested better) 1h candles, same EMA9/VWAP cross, but only
           when price is above EMA 200 (up-trend) and the signal candle closes
           above BOTH EMA9 and VWAP. Same exit: first close below EMA9 or 1.5% stop.

Every preset is just a set of parameters, so you can also build your own.
"""
from __future__ import annotations

import pandas as pd

ANCHORS = {
    "utc": ("UTC", "00:00"),
    "new_york": ("America/New_York", "09:30"),
}

DEFAULT_PARAMS = {
    "timeframe": "3m",
    "ema_period": 9,
    "trend_filter": "none",     # none | ema | vwap_slope | both
    "trend_ema": 200,           # length of the trend EMA
    "vwap_slope_bars": 5,       # VWAP must be higher than N candles ago (vwap_slope)
    "confirm": False,           # signal candle must close above EMA9 AND VWAP
    "exit_mode": "ema",         # ema = first close back through EMA9
                                # atr = ATR stop + take-profit at R multiple
                                # trail = ATR trailing stop
    "stop_loss_pct": 1.5,       # hard stop for exit_mode ema
    "take_profit_pct": 0,       # optional for exit_mode ema (0 = off)
    "atr_period": 14,
    "atr_stop_mult": 2.5,       # atr / trail: stop distance = mult x ATR
    "atr_tp_r": 3.0,            # atr: take profit = R x stop distance (0 = off)
    "trail_atr_mult": 3.0,      # trail: trailing distance = mult x ATR
    "cooldown_bars": 0,         # candles to wait after a stop loss
}

PRESETS = {
    "video": {},
    "trend": {"timeframe": "1h", "trend_filter": "ema", "trend_ema": 200, "confirm": True},
}


def resolve_params(cfg: dict) -> dict:
    """Preset defaults <- config.yaml strategies.<name> overrides."""
    name = str(cfg.get("strategy", "trend")).lower()
    user = (cfg.get("strategies") or {}).get(name) or {}
    if name not in PRESETS and not user:
        raise ValueError(f"Unknown strategy {name!r}. Use one of {list(PRESETS)} or define it under strategies:")
    p = {**DEFAULT_PARAMS, **PRESETS.get(name, {}), **{k: v for k, v in user.items() if v is not None}}
    p["name"] = name
    return p


def resolve_anchor(cfg: dict) -> tuple[str, str]:
    custom = cfg.get("vwap_anchor_custom") or {}
    if custom.get("timezone") and custom.get("time"):
        return custom["timezone"], str(custom["time"])
    name = str(cfg.get("vwap_anchor", "utc")).lower()
    if name not in ANCHORS:
        raise ValueError(f"vwap_anchor must be one of {list(ANCHORS)} (got {name!r})")
    return ANCHORS[name]


def ohlcv_to_df(ohlcv: list) -> pd.DataFrame:
    df = pd.DataFrame(ohlcv, columns=["ts", "open", "high", "low", "close", "volume"])
    df = df.drop_duplicates("ts").sort_values("ts").reset_index(drop=True)
    for c in ["open", "high", "low", "close", "volume"]:
        df[c] = df[c].astype(float)
    df["time"] = pd.to_datetime(df["ts"], unit="ms", utc=True)
    return df


def add_indicators(df: pd.DataFrame, ema_period: int = 9, anchor_tz: str = "UTC",
                   anchor_time: str = "00:00", params: dict | None = None) -> pd.DataFrame:
    """Adds: session, vwap, ema, cross_up, cross_down, ema_trend, atr, vwap_slope,
    and (if params given) long_signal, short_signal."""
    df = df.copy()
    hh, mm = (int(x) for x in anchor_time.split(":"))
    local = df["time"].dt.tz_convert(anchor_tz)
    shifted = local - pd.Timedelta(hours=hh, minutes=mm)  # session starts at the anchor time
    df["session"] = shifted.dt.strftime("%Y-%m-%d")

    tp = (df["high"] + df["low"] + df["close"]) / 3.0
    pv = tp * df["volume"]
    cum_pv = pv.groupby(df["session"]).cumsum()
    cum_v = df["volume"].groupby(df["session"]).cumsum()
    df["vwap"] = (cum_pv / cum_v.where(cum_v > 0)).ffill().fillna(df["close"])
    df["ema"] = df["close"].ewm(span=ema_period, adjust=False).mean()

    diff = df["ema"] - df["vwap"]
    prev = diff.shift(1)
    same_session = df["session"] == df["session"].shift(1)
    warm = pd.Series(range(len(df)), index=df.index) >= ema_period * 3
    df["cross_up"] = (prev <= 0) & (diff > 0) & same_session & warm
    df["cross_down"] = (prev >= 0) & (diff < 0) & same_session & warm

    p = {**DEFAULT_PARAMS, **(params or {})}
    df["ema_trend"] = df["close"].ewm(span=int(p["trend_ema"]), adjust=False).mean()
    pc = df["close"].shift(1)
    tr = pd.concat([df["high"] - df["low"], (df["high"] - pc).abs(), (df["low"] - pc).abs()], axis=1).max(axis=1)
    df["atr"] = tr.ewm(alpha=1 / int(p["atr_period"]), adjust=False).mean()
    df["vwap_slope"] = df["vwap"] - df["vwap"].shift(int(p["vwap_slope_bars"]))

    if params is not None:
        up_ok = pd.Series(True, index=df.index)
        dn_ok = pd.Series(True, index=df.index)
        tf = p["trend_filter"]
        if tf in ("ema", "ema200", "both"):
            up_ok &= df["close"] > df["ema_trend"]
            dn_ok &= df["close"] < df["ema_trend"]
        if tf in ("vwap_slope", "both"):
            up_ok &= df["vwap_slope"] > 0
            dn_ok &= df["vwap_slope"] < 0
        long_sig = df["cross_up"] & up_ok
        short_sig = df["cross_down"] & dn_ok
        if p["confirm"]:
            long_sig &= (df["close"] > df["ema"]) & (df["close"] > df["vwap"])
            short_sig &= (df["close"] < df["ema"]) & (df["close"] < df["vwap"])
        df["long_signal"] = long_sig
        df["short_signal"] = short_sig
    return df


def exit_signal(side: str, close: float, ema: float) -> bool:
    """Candle closed back through EMA9?"""
    if side == "long":
        return close < ema
    return close > ema


def stop_and_target(side: str, entry: float, stop_pct: float, tp_pct: float):
    """Percent-based stop/target (used by exit_mode ema)."""
    d = 1 if side == "long" else -1
    stop = entry * (1 - d * stop_pct / 100) if stop_pct > 0 else None
    tp = entry * (1 + d * tp_pct / 100) if tp_pct > 0 else None
    return stop, tp


def initial_stops(side: str, entry: float, atr: float, p: dict):
    """Stop and take-profit prices for a new position, for any exit_mode."""
    if p["exit_mode"] == "ema":
        return stop_and_target(side, entry, float(p["stop_loss_pct"]), float(p.get("take_profit_pct") or 0))
    d = 1 if side == "long" else -1
    mult = float(p["atr_stop_mult"] if p["exit_mode"] == "atr" else p["trail_atr_mult"])
    stop = entry - d * mult * atr
    tp = None
    if p["exit_mode"] == "atr" and float(p.get("atr_tp_r") or 0) > 0:
        tp = entry + d * float(p["atr_tp_r"]) * mult * atr
    return stop, tp


def update_trailing(side: str, pos: dict, close: float, atr: float, p: dict):
    """exit_mode trail: move the stop behind the best close. Returns new stop."""
    if side == "long":
        pos["best"] = max(pos.get("best", pos["entry"]), close)
        return max(pos["stop"], pos["best"] - float(p["trail_atr_mult"]) * atr)
    pos["best"] = min(pos.get("best", pos["entry"]), close)
    return min(pos["stop"], pos["best"] + float(p["trail_atr_mult"]) * atr)
