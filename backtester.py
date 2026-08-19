"""
CLI backtester — runs the SAME strategy/exit code as the live bot over historical
klines, with taker fees + slippage modeled. Use it to sanity-check config changes
before trusting them with money.

Usage:
  python backtester.py --symbols BTCUSDT,ETHUSDT,SOLUSDT --days 90
  python backtester.py --symbols BTCUSDT --days 180 --strategy breakout

Uses MAINNET public market data (no keys needed, no orders placed).
Limitations (be honest with yourself when reading results):
  - fills happen at candle closes with fixed slippage; no intrabar paths
  - the rolling-high dip window is candle-based here (live is tick-based)
  - maker orders are modeled as taker + slippage
"""
import argparse
import logging
import sys
import time
from datetime import datetime

from binance.client import Client

import risk
import strategies
from config import CFG, validate
from indicators import compute_indicators

logging.basicConfig(level=logging.WARNING, format="%(asctime)s [%(levelname)s] %(message)s")
log = logging.getLogger("backtester")

def interval_ms(iv):
    return int(iv[:-1]) * {"m": 60_000, "h": 3_600_000, "d": 86_400_000}[iv[-1]]

def _daily(trades, day):
    return sum(t["profit"] for t in trades
               if t["action"] == "SELL" and t.get("profit") is not None
               and t["time"][:10] == day)

def simulate(cfg, client, symbols, days, only):
    data = {}
    for s in symbols:
        try:
            rows = client.get_historical_klines(s, cfg["KLINE_INTERVAL"], f"{days} days ago UTC")
            if rows: data[s] = rows
            else: log.warning(f"{s}: no history returned")
        except Exception as e:
            log.warning(f"{s}: history fetch failed ({e})")
        time.sleep(0.4)
    symbols = list(data)
    if not symbols: return []
    log.info(f"Fetched {sum(len(v) for v in data.values())} candles for {len(symbols)} symbols")

    times = [k[0] for k in data[symbols[0]]]
    idx = {s: {k[0]: j for j, k in enumerate(rows)} for s, rows in data.items()}

    mtf = {}
    if cfg["MTF_ENABLED"]:
        for s in symbols:
            try:
                raw4 = client.get_historical_klines(s, cfg["MTF_INTERVAL"], f"{days + 5} days ago UTC")
            except Exception:
                raw4 = []
            time.sleep(0.4)
            c4 = [float(k[4]) for k in raw4]
            series = []
            for j in range(len(raw4)):
                if j + 1 >= cfg["MTF_MA_PERIOD"]:
                    ma = sum(c4[j + 1 - cfg["MTF_MA_PERIOD"]:j + 1]) / cfg["MTF_MA_PERIOD"]
                    series.append((raw4[j][0] + interval_ms(cfg["MTF_INTERVAL"]) - 1, ma))
            mtf[s] = series

    positions, trades = {}, []
    symbol_stats, cooldowns = {}, {}
    equity = cfg["STARTING_EQUITY"]
    state_stub = {"paused": False, "peak_equity": equity, "crash_guard_until": None, "cooldowns": cooldowns}
    warm = max(cfg["ROLLING_WINDOW"], 60)
    ptr = {s: 0 for s in symbols}
    slip = cfg["SLIPPAGE_BPS"] / 10000

    for t in times[warm:]:
        now = datetime.utcfromtimestamp(t / 1000)
        daily_pnl = _daily(trades, now.date().isoformat())
        for s in symbols:
            j = idx[s].get(t)
            if j is None or j < warm: continue
            raw = data[s]
            price = float(raw[j][4])
            closes = [float(k[4]) for k in raw]
            an = compute_indicators(raw[:j + 1], cfg)
            m_ok = True
            if cfg["MTF_ENABLED"]:
                series = mtf.get(s) or []
                while ptr[s] < len(series) and series[ptr[s]][0] <= t: ptr[s] += 1
                if ptr[s] > 0: m_ok = price >= series[ptr[s] - 1][1]

            if s in positions:
                pos = positions[s]
                act = strategies.evaluate_exits(pos, price, an, now, cfg)
                if not act: continue
                if act[0] == "partial":
                    qty = pos["qty"] * act[1]
                    px = price * (1 - slip); notional = qty * px; fee = notional * cfg["FEE_RATE"]
                    profit = (notional - fee) - pos["avg_cost"] * qty
                    pos["qty"] -= qty; pos["partial_taken"] = True
                    trades.append({"time": now.isoformat(), "action": "SELL", "symbol": s,
                                   "strategy": pos["strategy"], "price": px, "qty": qty,
                                   "notional": notional, "fee_usdt": fee, "profit": profit,
                                   "reason": act[2]})
                    if pos["qty"] <= 1e-12:
                        del positions[s]; cooldowns[s] = now.isoformat()
                        strategies.adaptive_update(symbol_stats, s, profit >= 0, cfg)
                else:
                    qty = pos["qty"]; px = price * (1 - slip)
                    notional = qty * px; fee = notional * cfg["FEE_RATE"]
                    profit = (notional - fee) - pos["avg_cost"] * qty
                    trades.append({"time": now.isoformat(), "action": "SELL", "symbol": s,
                                   "strategy": pos["strategy"], "price": px, "qty": qty,
                                   "notional": notional, "fee_usdt": fee, "profit": profit,
                                   "reason": act[1]})
                    del positions[s]; cooldowns[s] = now.isoformat()
                    strategies.adaptive_update(symbol_stats, s, profit >= 0, cfg)
                    equity += profit
            else:
                ok, _ = risk.entry_gate(cfg, state_stub, daily_pnl, equity, len(positions), s, now)
                if not ok or (cfg["MTF_ENABLED"] and not m_ok): continue
                ctx = {"symbol": s, "price": price, "an": an, "cfg": cfg,
                       "rolling_high": max(closes[j - cfg["ROLLING_WINDOW"]:j]),
                       "multiplier": strategies.adaptive_multiplier(symbol_stats, s, cfg)}
                for fn in strategies.ENTRY_STRATEGIES:
                    if only != "all" and fn.__name__.replace("entry_", "") != only: continue
                    sig = fn(ctx)
                    if not sig: continue
                    quote = risk.size_position(cfg, sig["stop_pct"], (an or {}).get("atr_pct"),
                                               equity, None, 0.0)
                    if not quote: break
                    px = price * (1 + slip); qty = quote / px
                    notional = qty * px; fee = notional * cfg["FEE_RATE"]
                    positions[s] = {"buy_price": px, "qty": qty, "avg_cost": (notional + fee) / qty,
                                    "timestamp": now.isoformat(), "peak_price": px,
                                    "trailing_active": False, "target_pct": sig["target_pct"],
                                    "stop_pct": sig["stop_pct"], "partial_taken": False,
                                    "ladder_step": 0, "be_armed": False, "strategy": sig["strategy"],
                                    "trail_activation_pct": sig.get("trail_activation_pct", cfg["TRAILING_ACTIVATION_PCT"]),
                                    "trail_stop_pct": sig.get("trail_stop_pct", cfg["TRAILING_STOP_PCT"])}
                    trades.append({"time": now.isoformat(), "action": "BUY", "symbol": s,
                                   "strategy": sig["strategy"], "price": px, "qty": qty,
                                   "notional": notional, "fee_usdt": fee, "profit": None,
                                   "reason": sig["reason"]})
                    break
    return trades

def report(trades, cfg, symbols, days, only):
    sells = [t for t in trades if t["action"] == "SELL" and t.get("profit") is not None]
    buys = [t for t in trades if t["action"] == "BUY"]
    print("\n" + "=" * 72)
    print(f"BACKTEST  {','.join(symbols)} | {cfg['KLINE_INTERVAL']} | {days}d | strategy: {only}")
    print(f"fees {cfg['FEE_RATE']*100:.2f}%/side + slippage {cfg['SLIPPAGE_BPS']}bps/side | "
          f"start equity ${cfg['STARTING_EQUITY']:.0f}")
    print("=" * 72)
    if not sells:
        print("No closed trades — loosen thresholds or extend the period.")
        return

    def block(name, subset):
        if not subset:
            print(f"\n{name}: no closed trades"); return
        total = sum(t["profit"] for t in subset)
        wins = [t for t in subset if t["profit"] > 0]
        gw = sum(t["profit"] for t in wins)
        gl = -sum(t["profit"] for t in subset if t["profit"] <= 0)
        pf = (gw / gl) if gl > 0 else float("inf")
        cum = peak = mdd = 0.0
        for t in sorted(subset, key=lambda x: x["time"]):
            cum += t["profit"]; peak = max(peak, cum); mdd = max(mdd, peak - cum)
        holds = []
        for t in subset:
            pass  # hold time needs pairing with buys; approximate skipped for brevity
        print(f"\n{name}")
        print(f"  closed trades : {len(subset)}")
        print(f"  net P/L       : ${total:,.2f}  ({total/cfg['STARTING_EQUITY']*100:.2f}% of start equity)")
        print(f"  win rate      : {len(wins)}/{len(subset)} = {len(wins)/len(subset)*100:.0f}%")
        print(f"  profit factor : {pf:.2f}")
        print(f"  max drawdown  : ${mdd:,.2f}")

    block("TOTAL", sells)
    for key in ("dip", "bb", "breakout"):
        block(f"strategy: {key}", [t for t in sells if t.get("strategy") == key])
    print(f"\nNote: {len(buys)} buys, {len(sells)} closed. Any open position at the end is excluded.")
    print("Caveats: candle-close fills, no intrabar paths, maker modeled as taker+slippage.")

def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--symbols", default="BTCUSDT,ETHUSDT,SOLUSDT")
    ap.add_argument("--days", type=int, default=90)
    ap.add_argument("--strategy", default="all", choices=["all", "dip", "bb", "breakout"])
    args = ap.parse_args()

    errors, warns = validate(strict=False)
    for w in warns: print(f"WARNING: {w}")
    if errors:
        for e in errors: print(f"CONFIG ERROR: {e}")
        sys.exit(1)

    client = Client(CFG["API_KEY"] or None, CFG["API_SECRET"] or None)  # mainnet public data
    symbols = [s.strip().upper() for s in args.symbols.split(",") if s.strip()]
    trades = simulate(CFG, client, symbols, args.days, args.strategy)
    report(trades, CFG, symbols, args.days, args.strategy)

if __name__ == "__main__":
    main()