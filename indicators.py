"""Pure indicator math. No network, no state — shared by live bot and backtester."""
import statistics

def sma(values, period):
    if len(values) < period: return None
    return sum(values[-period:]) / period

def rsi(closes, period=14):
    if len(closes) < period + 1: return None
    gains, losses = [], []
    for i in range(1, len(closes)):
        ch = closes[i] - closes[i - 1]
        gains.append(max(ch, 0)); losses.append(max(-ch, 0))
    ag = sum(gains[-period:]) / period
    al = sum(losses[-period:]) / period
    if al == 0: return 100.0
    return 100 - 100 / (1 + ag / al)

def atr_pct(raw, period=14):
    """Average True Range as % of last price."""
    if len(raw) < period + 1: return None
    highs = [float(k[2]) for k in raw]; lows = [float(k[3]) for k in raw]; closes = [float(k[4]) for k in raw]
    trs = [max(highs[i] - lows[i], abs(highs[i] - closes[i - 1]), abs(lows[i] - closes[i - 1]))
           for i in range(1, len(raw))]
    atr = sum(trs[-period:]) / period
    return (atr / closes[-1] * 100) if closes[-1] else None

def bollinger(closes, period=20, std=2.0):
    if len(closes) < period: return None
    w = closes[-period:]
    mid = sum(w) / period
    sd = statistics.pstdev(w)
    return mid, mid + std * sd, mid - std * sd

def donchian(raw, lookback):
    """Channel from COMPLETED candles only (excludes the forming one)."""
    if len(raw) <= lookback: return None
    highs = [float(k[2]) for k in raw[:-1][-lookback:]]
    lows = [float(k[3]) for k in raw[:-1][-lookback:]]
    return max(highs), min(lows)

def volume_ratio(raw, period=20):
    vols = [float(k[5]) for k in raw]
    if len(vols) < period + 1: return None
    avg = sma(vols[:-1], period)
    return vols[-1] / avg if avg else None

def compute_indicators(raw, cfg):
    """One klines fetch -> every indicator all strategies need.
    RSI / trend MA include the forming candle (matches the original bot's behaviour);
    Bollinger / Donchian use completed candles only."""
    if not raw: return None
    closes = [float(k[4]) for k in raw]
    an = {
        "rsi": rsi(closes, cfg["RSI_PERIOD"]),
        "trend_ma": sma(closes, cfg["TREND_MA_PERIOD"]),
        "atr_pct": atr_pct(raw, cfg["ATR_PERIOD"]),
        "volume_ratio": volume_ratio(raw, cfg["VOLUME_MA_PERIOD"]),
    }
    d = donchian(raw, cfg["BREAKOUT_LOOKBACK"])
    if d: an["don_high"], an["don_low"] = d
    b = bollinger(closes[:-1], cfg["BB_PERIOD"], cfg["BB_STD"])
    if b: an["bb_mid"], an["bb_upper"], an["bb_lower"] = b
    return an