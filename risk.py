"""Portfolio risk: entry gates (never blocks EXITS), position sizing, crash guard."""
import logging
from datetime import datetime, timedelta

log = logging.getLogger("dipbot")

def entry_gate(cfg, state, daily_pnl, equity, n_positions, symbol, now):
    """(ok, reason). Checks everything that doesn't need market data, so the
    caller can skip the klines fetch entirely when entries are blocked."""
    if state.get("paused"): return False, "entries paused"
    if daily_pnl is not None and daily_pnl <= -abs(cfg["DAILY_MAX_LOSS_USDT"]):
        return False, f"daily loss limit (-${cfg['DAILY_MAX_LOSS_USDT']:.0f})"
    peak = state.get("peak_equity")
    if peak and equity and (peak - equity) / peak * 100 >= cfg["MAX_DRAWDOWN_PCT"]:
        return False, f"drawdown breaker ({cfg['MAX_DRAWDOWN_PCT']}%)"
    cg = state.get("crash_guard_until")
    if cg and now < datetime.fromisoformat(cg):
        return False, "crash guard active"
    cd = state.get("cooldowns", {}).get(symbol)
    if cd and now < datetime.fromisoformat(cd) + timedelta(minutes=cfg["ENTRY_COOLDOWN_MIN"]):
        return False, "symbol cooldown"
    if n_positions >= cfg["MAX_CONCURRENT_POSITIONS"]:
        return False, "capacity full"
    return True, ""

def exposure_ok(cfg, equity, open_notional, add_usdt):
    if not equity or cfg["MAX_TOTAL_EXPOSURE_PCT"] <= 0: return True
    return open_notional + add_usdt <= equity * cfg["MAX_TOTAL_EXPOSURE_PCT"] / 100

def capital_cap_ok(cfg, open_notional, add_usdt):
    """
    Hard absolute ceiling on total capital the bot deploys, independent of
    account equity (which on testnet includes faucet-granted balances worth
    far more than the user's actual intended budget). Once open positions'
    total value reaches MAX_BOT_CAPITAL_USDT, no new entries are allowed —
    trading only resumes as existing positions close and free up room.
    """
    cap = cfg["MAX_BOT_CAPITAL_USDT"]
    if cap <= 0:
        return True  # cap disabled
    return open_notional + add_usdt <= cap

def size_position(cfg, stop_pct, atr_pct, equity, usdt_free, min_notional):
    """Risk-based sizing: position shrinks so each trade risks ~RISK_PER_TRADE_PCT
    of equity at its stop distance, with an inverse-volatility cap and hard
    caps from equity / free USDT / configured max. Returns USDT quote or None."""
    base = cfg["POSITION_SIZE_USDT"]
    if equity:
        base = min(base, equity * cfg["MAX_POSITION_PCT"] / 100)
        if cfg["RISK_PER_TRADE_PCT"] > 0 and stop_pct > 0:
            base = min(base, (equity * cfg["RISK_PER_TRADE_PCT"] / 100) / (stop_pct / 100))
    if usdt_free is not None:
        base = min(base, usdt_free * 0.98)
    if atr_pct and atr_pct > 0:            # a 4%-ATR coin gets half the size of a 2%-ATR coin
        base *= min(1.0, 2.0 / atr_pct)
    base = round(base, 2)
    if base < min_notional: return None
    return base

def check_crash_guard(ex, cfg, state, now):
    """Market-wide correlation proxy: if BTC drops hard in the last hour, most
    alt dips are the SAME event — block new entries for a cooldown window."""
    if not cfg["CRASH_GUARD_ENABLED"]: return
    raw = ex.get_klines("BTCUSDT", "1h", 2)
    if not raw or len(raw) < 2: return
    prev, last = float(raw[-2][4]), float(raw[-1][4])
    if prev > 0 and (last - prev) / prev * 100 <= -abs(cfg["CRASH_GUARD_PCT"]):
        until = (now + timedelta(minutes=cfg["CRASH_GUARD_COOLDOWN_MIN"])).isoformat()
        if state.get("crash_guard_until") != until:
            log.warning(f"CRASH GUARD: BTC {(last-prev)/prev*100:.2f}% in 1h — new entries blocked for "
                        f"{cfg['CRASH_GUARD_COOLDOWN_MIN']} min (open positions still managed)")
        state["crash_guard_until"] = until