"""Pure decision logic — no network, no orders. Both the live bot and the
backtester call these exact functions, so backtest results and live behaviour
stay as close as identical as possible."""
import logging
from datetime import datetime

log = logging.getLogger("dipbot")

# ---------- adaptive per-symbol tuning (shared live + backtest) ----------

def adaptive_multiplier(symbol_stats, symbol, cfg):
    if not cfg["ADAPTIVE_ENABLED"]: return 1.0
    st = symbol_stats.get(symbol)
    return st.get("multiplier", 1.0) if st else 1.0

def adaptive_update(symbol_stats, symbol, was_win, cfg):
    if not cfg["ADAPTIVE_ENABLED"]: return
    st = symbol_stats.setdefault(symbol, {"multiplier": 1.0, "recent_results": []})
    st["recent_results"].append(1 if was_win else 0)
    st["recent_results"] = st["recent_results"][-cfg["ADAPTIVE_LOOKBACK_TRADES"]:]
    wins = sum(st["recent_results"]); total = len(st["recent_results"])
    wr = wins / total if total else 0.5
    drift = (0.5 - wr) * 2 * cfg["ADAPTIVE_STEP"]
    st["multiplier"] = max(cfg["ADAPTIVE_MIN_MULTIPLIER"],
                           min(cfg["ADAPTIVE_MAX_MULTIPLIER"], st["multiplier"] + drift))
    log.info(f"{symbol}: adaptive -> {st['multiplier']:.2f}x (last {total} trades {wr*100:.0f}% wins)")

# ---------- entries ----------

def _dip_thresholds(ctx):
    cfg, an = ctx["cfg"], ctx["an"]
    if cfg["ATR_ENABLED"] and an and an.get("atr_pct") is not None:
        a = an["atr_pct"]
        dip = max(a * cfg["ATR_DIP_MULTIPLIER"], cfg["MIN_DIP_PCT"])
        target = max(a * cfg["ATR_TARGET_MULTIPLIER"], cfg["MIN_TARGET_PCT"])
        stop = max(a * cfg["ATR_STOP_MULTIPLIER"], cfg["MIN_STOP_PCT"])
    else:
        dip, target, stop = cfg["DIP_THRESHOLD_PCT"], cfg["SELL_TARGET_PCT"], cfg["STOP_LOSS_PCT"]
    dip *= ctx.get("multiplier", 1.0)
    return dip, target, stop

def entry_dip(ctx):
    """Oversold dip in an uptrend, volume-confirmed."""
    cfg, an, price = ctx["cfg"], ctx["an"], ctx["price"]
    if not cfg["STRATEGY_DIP_ENABLED"] or not an: return None
    dip, target, stop = _dip_thresholds(ctx)
    high = ctx.get("rolling_high")
    if not high: return None
    drop = (high - price) / high * 100
    if drop < dip: return None
    rsi = an.get("rsi")
    if rsi is None or rsi > cfg["RSI_OVERSOLD"]: return None
    if cfg["TREND_FILTER_ENABLED"] and an.get("trend_ma") and price < an["trend_ma"]: return None
    vr = an.get("volume_ratio")
    if cfg["VOLUME_FILTER_ENABLED"] and vr is not None and vr < cfg["VOLUME_MULTIPLIER"]: return None
    reason = f"dip {drop:.2f}% (thr {dip:.2f}%) + RSI {rsi:.1f}"
    if vr: reason += f" + vol {vr:.1f}x"
    return {"strategy": "dip", "reason": reason, "target_pct": target, "stop_pct": stop}

def entry_bb(ctx):
    """Bollinger mean-reversion: buy a close below the lower band, exit at the mid band."""
    cfg, an, price = ctx["cfg"], ctx["an"], ctx["price"]
    if not cfg["STRATEGY_BB_ENABLED"] or not an or "bb_lower" not in an: return None
    if price >= an["bb_lower"]: return None
    rsi = an.get("rsi")
    if rsi is None or rsi > cfg["BB_RSI_MAX"]: return None
    # skip falling knives: deep below the trend MA means the band break may be a real collapse
    if an.get("trend_ma") and price < an["trend_ma"] * (1 - cfg["BB_MAX_BELOW_TREND_PCT"] / 100): return None
    atr = an.get("atr_pct") or 1.0
    target = max((an["bb_mid"] - price) / price * 100, cfg["MIN_TARGET_PCT"])
    stop = max(atr * cfg["ATR_STOP_MULTIPLIER"], cfg["MIN_STOP_PCT"])
    return {"strategy": "bb",
            "reason": f"below lower Bollinger band ({an['bb_lower']:.6g}) + RSI {rsi:.1f}",
            "target_pct": target, "stop_pct": stop}

def entry_breakout(ctx):
    """Donchian breakout: new high on strong volume; ATR-scaled trail rides the trend."""
    cfg, an, price = ctx["cfg"], ctx["an"], ctx["price"]
    if not cfg["STRATEGY_BREAKOUT_ENABLED"] or not an or "don_high" not in an: return None
    if price <= an["don_high"]: return None
    vr = an.get("volume_ratio")
    if vr is None or vr < cfg["BREAKOUT_VOLUME_MULT"]: return None
    rsi = an.get("rsi")
    if rsi is None or not (cfg["BREAKOUT_RSI_MIN"] <= rsi <= cfg["BREAKOUT_RSI_MAX"]): return None
    if cfg["TREND_FILTER_ENABLED"] and an.get("trend_ma") and price < an["trend_ma"]: return None
    atr = an.get("atr_pct") or 1.0
    return {"strategy": "breakout",
            "reason": f"breakout > {cfg['BREAKOUT_LOOKBACK']}-candle high ({an['don_high']:.6g}) + vol {vr:.1f}x + RSI {rsi:.0f}",
            "target_pct": max(atr * cfg["BREAKOUT_TARGET_ATR_MULT"], cfg["MIN_TARGET_PCT"] * 2),
            "stop_pct": max(atr * cfg["BREAKOUT_STOP_ATR_MULT"], cfg["MIN_STOP_PCT"]),
            "trail_activation_pct": max(atr * cfg["BREAKOUT_TRAIL_ACT_ATR_MULT"], 1.0),
            "trail_stop_pct": max(atr * cfg["BREAKOUT_TRAIL_ATR_MULT"], 0.8)}

ENTRY_STRATEGIES = [entry_dip, entry_bb, entry_breakout]

# ---------- exits ----------

def evaluate_exits(pos, price, an, now, cfg):
    """Pure exit logic. Mutates pos (peak, ladder step, breakeven flag) but places
    no orders. Returns at most ONE action per call:
        ("sell", reason)  or  ("partial", fraction, reason)  or  None
    Caller executes it; the next evaluation handles whatever comes next."""
    buy = pos["buy_price"]
    if price > pos.get("peak_price", buy): pos["peak_price"] = price
    gain = (price - buy) / buy * 100
    target = pos.get("target_pct", cfg["SELL_TARGET_PCT"])
    stop = pos.get("stop_pct", cfg["STOP_LOSS_PCT"])
    strat = pos.get("strategy", "dip")
    trail_act = pos.get("trail_activation_pct", cfg["TRAILING_ACTIVATION_PCT"])
    trail = pos.get("trail_stop_pct", cfg["TRAILING_STOP_PCT"])

    # 1. Bollinger positions exit at the mid band
    if strat == "bb" and an and an.get("bb_mid") and price >= an["bb_mid"]:
        return ("sell", f"BB mid-band reached ({an['bb_mid']:.6g})")

    # 2. take-profit: laddered if enabled, else partial, else plain full exit
    if cfg["LADDER_ENABLED"]:
        for i, (mult, frac) in enumerate(cfg["LADDER_TAKES"]):
            if pos.get("ladder_step", 0) == i and gain >= target * mult:
                pos["ladder_step"] = i + 1
                return ("partial", frac, f"ladder {i+1}/{len(cfg['LADDER_TAKES'])} (+{gain:.2f}%)")
    elif cfg["PARTIAL_TAKE_PROFIT_ENABLED"] and not pos.get("partial_taken") and gain >= target:
        pos["partial_taken"] = True
        return ("partial", cfg["PARTIAL_TAKE_PROFIT_FRACTION"], f"target {target:.2f}% hit")
    elif not cfg["PARTIAL_TAKE_PROFIT_ENABLED"] and gain >= target:
        return ("sell", "target hit")

    # 3. arm breakeven once up >= TRIGGER_R risk-units (R = stop distance)
    if cfg["BREAKEVEN_ENABLED"] and not pos.get("be_armed") and gain >= stop * cfg["BREAKEVEN_TRIGGER_R"]:
        pos["be_armed"] = True
        log.info(f"{pos.get('_symbol','?')}: breakeven armed at +{gain:.2f}%")

    # 4. trailing stop
    if cfg["TRAILING_STOP_ENABLED"]:
        if not pos.get("trailing_active") and gain >= trail_act:
            pos["trailing_active"] = True
        if pos.get("trailing_active") and price <= pos["peak_price"] * (1 - trail / 100):
            return ("sell", f"trailing stop (peak {pos['peak_price']:.6g})")

    # 5. stop-loss (floored at breakeven + round-trip fees once armed)
    stop_price = buy * (1 - stop / 100)
    if pos.get("be_armed"):
        stop_price = max(stop_price, buy * (1 + 2 * cfg["FEE_RATE"]))
    if price <= stop_price:
        return ("sell", "breakeven stop" if stop_price >= buy else "stop-loss")

    # 6. max hold
    try:
        held = (now - datetime.fromisoformat(pos["timestamp"])).total_seconds() / 3600
    except Exception:
        held = 0.0
    if held >= cfg["MAX_HOLD_HOURS"]:
        return ("sell", "max hold time exceeded")
    return None