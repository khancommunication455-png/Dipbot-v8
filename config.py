import os

def _s(k, d): return os.getenv(k, d)
def _f(k, d): return float(os.getenv(k, str(d)))
def _i(k, d): return int(os.getenv(k, str(d)))
def _b(k, d): return os.getenv(k, str(d)).lower() in ("1", "true", "yes", "on")
def _list(k, d): return [x.strip() for x in os.getenv(k, d).split(",") if x.strip()]
def _ladder(k, d):
    out = []
    for part in os.getenv(k, d).split(","):
        if ":" in part:
            m, f = part.split(":")
            try: out.append([float(m), float(f)])
            except ValueError: pass
    return out or [[1.0, 0.34], [2.0, 0.34]]

CFG = {
    # --- exchange / app ---
    "API_KEY": _s("BINANCE_API_KEY", ""),
    "API_SECRET": _s("BINANCE_API_SECRET", ""),
    "USE_TESTNET": _b("USE_TESTNET", True),
    "STATE_DB": _s("STATE_DB", "bot.db"),
    "PORT": _i("PORT", 10000),
    "CONTROL_TOKEN": _s("CONTROL_TOKEN", ""),
    "DASHBOARD_LOG_BUFFER": _i("DASHBOARD_LOG_BUFFER", 200),

    # --- universe / cadence ---
    "WATCHLIST": _list("WATCHLIST", "BTCUSDT,ETHUSDT,SOLUSDT,BNBUSDT,DOGEUSDT"),
    "MARKET_SCAN_ENABLED": _b("MARKET_SCAN_ENABLED", False),
    "MARKET_SCAN_TOP_N": _i("MARKET_SCAN_TOP_N", 30),
    "MARKET_SCAN_REFRESH_LOOPS": _i("MARKET_SCAN_REFRESH_LOOPS", 15),
    "MARKET_SCAN_MIN_VOLUME_USDT": _f("MARKET_SCAN_MIN_VOLUME_USDT", 5_000_000),
    "MARKET_SCAN_EXCLUDE": set(_list("MARKET_SCAN_EXCLUDE", "USDCUSDT,FDUSDUSDT,TUSDUSDT,BUSDUSDT,DAIUSDT")),
    "CHECK_INTERVAL_SEC": _i("CHECK_INTERVAL_SEC", 60),
    "KLINE_INTERVAL": _s("KLINE_INTERVAL", "15m"),
    "ROLLING_WINDOW": _i("ROLLING_WINDOW", 20),
    "MAX_HOLD_HOURS": _f("MAX_HOLD_HOURS", 24),
    "MAX_CONCURRENT_POSITIONS": _i("MAX_CONCURRENT_POSITIONS", 3),
    "POSITION_SIZE_USDT": _f("POSITION_SIZE_USDT", 10),

    # --- hard capital cap (independent of account equity) ---
    # On testnet, get_account() returns ~35 free faucet coins worth thousands of
    # fake dollars, which the equity-based exposure_ok() check would otherwise use.
    # This is a separate, absolute ceiling on how much the BOT ITSELF will ever
    # have deployed across open positions — once open positions total this amount,
    # no new entries fire, regardless of what the wallet's total equity says.
    "MAX_BOT_CAPITAL_USDT": _f("MAX_BOT_CAPITAL_USDT", 30),

    # --- fees / execution ---
    "FEE_RATE": _f("FEE_RATE", 0.001),
    "MAKER_ORDERS_ENABLED": _b("MAKER_ORDERS_ENABLED", False),
    "MAKER_WAIT_SEC": _i("MAKER_WAIT_SEC", 20),
    "ENTRY_COOLDOWN_MIN": _f("ENTRY_COOLDOWN_MIN", 30),

    # --- multi-timeframe confirmation ---
    "MTF_ENABLED": _b("MTF_ENABLED", True),
    "MTF_INTERVAL": _s("MTF_INTERVAL", "4h"),
    "MTF_MA_PERIOD": _i("MTF_MA_PERIOD", 50),

    # --- strategies on/off ---
    "STRATEGY_DIP_ENABLED": _b("STRATEGY_DIP_ENABLED", True),
    "STRATEGY_BB_ENABLED": _b("STRATEGY_BB_ENABLED", True),
    "STRATEGY_BREAKOUT_ENABLED": _b("STRATEGY_BREAKOUT_ENABLED", True),

    # --- dip strategy ---
    "DIP_THRESHOLD_PCT": _f("DIP_THRESHOLD_PCT", 2.0),
    "SELL_TARGET_PCT": _f("SELL_TARGET_PCT", 2.5),
    "STOP_LOSS_PCT": _f("STOP_LOSS_PCT", 3.0),
    "RSI_PERIOD": _i("RSI_PERIOD", 14),
    "RSI_OVERSOLD": _f("RSI_OVERSOLD", 35),
    "TREND_MA_PERIOD": _i("TREND_MA_PERIOD", 50),
    "TREND_FILTER_ENABLED": _b("TREND_FILTER_ENABLED", True),
    "VOLUME_FILTER_ENABLED": _b("VOLUME_FILTER_ENABLED", True),
    "VOLUME_MA_PERIOD": _i("VOLUME_MA_PERIOD", 20),
    "VOLUME_MULTIPLIER": _f("VOLUME_MULTIPLIER", 1.2),

    # --- ATR adaptive ---
    "ATR_ENABLED": _b("ATR_ENABLED", True),
    "ATR_PERIOD": _i("ATR_PERIOD", 14),
    "ATR_DIP_MULTIPLIER": _f("ATR_DIP_MULTIPLIER", 1.0),
    "ATR_TARGET_MULTIPLIER": _f("ATR_TARGET_MULTIPLIER", 1.5),
    "ATR_STOP_MULTIPLIER": _f("ATR_STOP_MULTIPLIER", 1.8),
    "MIN_DIP_PCT": _f("MIN_DIP_PCT", 0.8),
    "MIN_TARGET_PCT": _f("MIN_TARGET_PCT", 1.0),
    "MIN_STOP_PCT": _f("MIN_STOP_PCT", 1.5),

    # --- exits: trailing / partial / ladder / breakeven ---
    "TRAILING_STOP_ENABLED": _b("TRAILING_STOP_ENABLED", True),
    "TRAILING_ACTIVATION_PCT": _f("TRAILING_ACTIVATION_PCT", 1.5),
    "TRAILING_STOP_PCT": _f("TRAILING_STOP_PCT", 1.0),
    "PARTIAL_TAKE_PROFIT_ENABLED": _b("PARTIAL_TAKE_PROFIT_ENABLED", True),
    "PARTIAL_TAKE_PROFIT_FRACTION": _f("PARTIAL_TAKE_PROFIT_FRACTION", 0.5),
    "LADDER_ENABLED": _b("LADDER_ENABLED", True),
    "LADDER_TAKES": _ladder("LADDER_TAKES", "1.0:0.34,2.0:0.34"),
    "BREAKEVEN_ENABLED": _b("BREAKEVEN_ENABLED", True),
    "BREAKEVEN_TRIGGER_R": _f("BREAKEVEN_TRIGGER_R", 1.0),

    # --- bollinger reversion ---
    "BB_PERIOD": _i("BB_PERIOD", 20),
    "BB_STD": _f("BB_STD", 2.0),
    "BB_RSI_MAX": _f("BB_RSI_MAX", 45),
    "BB_MAX_BELOW_TREND_PCT": _f("BB_MAX_BELOW_TREND_PCT", 3.0),

    # --- donchian breakout ---
    "BREAKOUT_LOOKBACK": _i("BREAKOUT_LOOKBACK", 24),
    "BREAKOUT_VOLUME_MULT": _f("BREAKOUT_VOLUME_MULT", 2.0),
    "BREAKOUT_RSI_MIN": _f("BREAKOUT_RSI_MIN", 55),
    "BREAKOUT_RSI_MAX": _f("BREAKOUT_RSI_MAX", 80),
    "BREAKOUT_STOP_ATR_MULT": _f("BREAKOUT_STOP_ATR_MULT", 1.5),
    "BREAKOUT_TARGET_ATR_MULT": _f("BREAKOUT_TARGET_ATR_MULT", 4.0),
    "BREAKOUT_TRAIL_ACT_ATR_MULT": _f("BREAKOUT_TRAIL_ACT_ATR_MULT", 1.0),
    "BREAKOUT_TRAIL_ATR_MULT": _f("BREAKOUT_TRAIL_ATR_MULT", 0.9),

    # --- adaptive per-symbol tuning ---
    "ADAPTIVE_ENABLED": _b("ADAPTIVE_ENABLED", True),
    "ADAPTIVE_LOOKBACK_TRADES": _i("ADAPTIVE_LOOKBACK_TRADES", 5),
    "ADAPTIVE_STEP": _f("ADAPTIVE_STEP", 0.1),
    "ADAPTIVE_MIN_MULTIPLIER": _f("ADAPTIVE_MIN_MULTIPLIER", 0.7),
    "ADAPTIVE_MAX_MULTIPLIER": _f("ADAPTIVE_MAX_MULTIPLIER", 1.5),

    # --- portfolio risk ---
    "DAILY_MAX_LOSS_USDT": _f("DAILY_MAX_LOSS_USDT", 15.0),
    "MAX_DRAWDOWN_PCT": _f("MAX_DRAWDOWN_PCT", 15.0),
    "CRASH_GUARD_ENABLED": _b("CRASH_GUARD_ENABLED", True),
    "CRASH_GUARD_PCT": _f("CRASH_GUARD_PCT", 3.0),
    "CRASH_GUARD_COOLDOWN_MIN": _f("CRASH_GUARD_COOLDOWN_MIN", 60),
    "RISK_PER_TRADE_PCT": _f("RISK_PER_TRADE_PCT", 1.0),
    "MAX_POSITION_PCT": _f("MAX_POSITION_PCT", 15.0),
    "MAX_TOTAL_EXPOSURE_PCT": _f("MAX_TOTAL_EXPOSURE_PCT", 60.0),

    # --- telegram ---
    "TELEGRAM_BOT_TOKEN": _s("TELEGRAM_BOT_TOKEN", ""),
    "TELEGRAM_CHAT_ID": _s("TELEGRAM_CHAT_ID", ""),
    "TELEGRAM_COMMANDS_ENABLED": _b("TELEGRAM_COMMANDS_ENABLED", True),

    # --- discord (optional, alerts only) ---
    # Simple webhook — no bot token/hosting needed. One-way: Discord gets the
    # same alerts Telegram would, but remote commands (/pause, /close, etc.)
    # still need Telegram since those require a live two-way bot connection
    # a webhook can't provide.
    "DISCORD_WEBHOOK_URL": _s("DISCORD_WEBHOOK_URL", ""),

    # --- backtester-only ---
    "SLIPPAGE_BPS": _f("SLIPPAGE_BPS", 5),
    "STARTING_EQUITY": _f("STARTING_EQUITY", 1000),
}

# one klines fetch must cover the widest indicator window
CFG["INDICATOR_LIMIT"] = max(
    CFG["TREND_MA_PERIOD"], CFG["RSI_PERIOD"] + 1, CFG["VOLUME_MA_PERIOD"] + 1,
    CFG["ATR_PERIOD"] + 1, CFG["BREAKOUT_LOOKBACK"] + 1, CFG["BB_PERIOD"] + 1,
) + 10

def validate(strict=True):
    """Returns (errors, warnings). Bot exits on errors; backtester only warns."""
    c, errors, warns = CFG, [], []
    if strict:
        if not c["API_KEY"] or not c["API_SECRET"]:
            errors.append("BINANCE_API_KEY / BINANCE_API_SECRET missing")
        if not (c["STRATEGY_DIP_ENABLED"] or c["STRATEGY_BB_ENABLED"] or c["STRATEGY_BREAKOUT_ENABLED"]):
            errors.append("all strategies are disabled")
    f = c["PARTIAL_TAKE_PROFIT_FRACTION"]
    if not (0 < f <= 1): errors.append(f"PARTIAL_TAKE_PROFIT_FRACTION must be in (0,1], got {f}")
    total = 0.0
    for m, fr in c["LADDER_TAKES"]:
        if not (0 < fr < 1): errors.append(f"ladder fraction {fr} must be in (0,1)")
        if m <= 0: errors.append(f"ladder multiple {m} must be > 0")
        total += fr
    if c["LADDER_ENABLED"] and total >= 1:
        errors.append("ladder fractions sum to >= 1 — must leave a runner to trail")
    if c["BREAKEVEN_TRIGGER_R"] <= 0: errors.append("BREAKEVEN_TRIGGER_R must be > 0")
    for k in ("ATR_DIP_MULTIPLIER", "ATR_TARGET_MULTIPLIER", "ATR_STOP_MULTIPLIER",
              "TRAILING_STOP_PCT", "STOP_LOSS_PCT", "MAX_HOLD_HOURS", "TRAILING_ACTIVATION_PCT"):
        if c[k] <= 0: errors.append(f"{k} must be > 0")
    if c["POSITION_SIZE_USDT"] < 5:
        warns.append("POSITION_SIZE_USDT below typical exchange minimum ($5) — orders will be skipped")
    if c["MAX_BOT_CAPITAL_USDT"] <= 0:
        errors.append("MAX_BOT_CAPITAL_USDT must be > 0")
    if c["POSITION_SIZE_USDT"] > c["MAX_BOT_CAPITAL_USDT"]:
        warns.append(
            f"POSITION_SIZE_USDT (${c['POSITION_SIZE_USDT']}) exceeds MAX_BOT_CAPITAL_USDT "
            f"(${c['MAX_BOT_CAPITAL_USDT']}) — even a single position would breach the cap"
        )
    if not c["USE_TESTNET"]:
        warns.append("USE_TESTNET=false — LIVE TRADING with real money")
        if not c["CONTROL_TOKEN"]:
            warns.append("CONTROL_TOKEN not set — manual-close endpoints are unprotected")
    if c["MAKER_ORDERS_ENABLED"] and c["MAKER_WAIT_SEC"] < 5:
        warns.append("MAKER_WAIT_SEC < 5s will rarely fill as maker")
    return errors, warns