"""
Multi-Strategy Trading Bot for Binance — v9
Modular rewrite: config / indicators / strategies / execution / datastore / risk /
notify / dashboard. The strategy + exit logic in strategies.py is PURE — the
backtester runs the exact same code paths the live bot runs.

Runs on Binance TESTNET by default. No strategy guarantees profit.
"""
import logging
import os
import signal
import threading
import time
from collections import deque
from datetime import datetime, timedelta

from binance.client import Client
from binance.exceptions import BinanceAPIException

import dashboard
import datastore
import notify as notify_mod
import risk
import strategies
from config import CFG, validate
from execution import Exchange
from indicators import compute_indicators

# ---------------- logging (buffer feeds the dashboard) ----------------
_log_buffer = deque(maxlen=CFG["DASHBOARD_LOG_BUFFER"])
_log_lock = threading.Lock()

class BufferHandler(logging.Handler):
    def emit(self, record):
        try:
            with _log_lock:
                _log_buffer.append({"time": datetime.utcnow().isoformat(),
                                    "level": record.levelname,
                                    "message": self.format(record)})
        except Exception:
            pass

logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")
log = logging.getLogger("dipbot")
_bh = BufferHandler(); _bh.setFormatter(logging.Formatter("%(message)s")); log.addHandler(_bh)

# ---------------- shared runtime state (dashboard reads this) ----------------
shared = {
    "started_at": datetime.utcnow().isoformat(), "last_check": None, "loops": 0,
    "last_prices": {}, "watchlist": [], "equity": None, "usdt_free": None,
    "peak_equity": None, "paused": False, "crash_guard_until": None,
}
state_lock = threading.RLock()
_shutdown = {"flag": False}

# ---------------- trade execution (single source of truth) ----------------

def do_buy(ex, store, state, cfg, notify, symbol, sig, quote_qty, now=None):
    now = now or datetime.utcnow()
    fill = ex.smart_buy(symbol, quote_qty, cfg)
    if not fill or fill["qty"] <= 0:
        log.error(f"{symbol}: buy failed or zero fill — no position tracked")
        return False
    state["positions"][symbol] = {
        "buy_price": fill["price"], "qty": fill["qty"],
        "avg_cost": fill["cost"] / fill["qty"], "buy_cost": fill["cost"],
        "timestamp": now.isoformat(), "peak_price": fill["price"],
        "trailing_active": False, "target_pct": sig["target_pct"], "stop_pct": sig["stop_pct"],
        "partial_taken": False, "ladder_step": 0, "be_armed": False,
        "strategy": sig["strategy"],
        "trail_activation_pct": sig.get("trail_activation_pct", cfg["TRAILING_ACTIVATION_PCT"]),
        "trail_stop_pct": sig.get("trail_stop_pct", cfg["TRAILING_STOP_PCT"]),
    }
    store.log_trade({"time": now.isoformat(), "action": "BUY", "symbol": symbol,
                     "strategy": sig["strategy"], "price": fill["price"], "qty": fill["qty"],
                     "notional": fill["notional"], "fee_usdt": fill["fee_usdt"],
                     "profit": None, "reason": sig["reason"]})
    log.info(f"BUY {symbol} @ {fill['price']:.6g} (notional ${fill['notional']:.2f}, fee ${fill['fee_usdt']:.4f}) "
             f"via [{sig['strategy']}] | {sig['reason']}")
    notify.send(f"🟢 BUY {symbol} @ {fill['price']:.6g}\nstrategy: {sig['strategy']}\n"
                f"size: ${fill['notional']:.2f}\n{sig['reason']}\n"
                f"target {sig['target_pct']:.2f}% / stop {sig['stop_pct']:.2f}%")
    return True

def do_sell(ex, store, state, cfg, notify, symbol, reason, now=None):
    now = now or datetime.utcnow()
    pos = state["positions"].get(symbol)
    if not pos: return None
    qty = ex.round_to_step(pos["qty"], ex.lot_step(symbol))
    if qty <= 0:
        log.error(f"{symbol}: sell qty rounded to 0 — dropping untracked position")
        del state["positions"][symbol]
        return None
    fill = ex.market_sell(symbol, qty)   # exits always go out fast as market orders
    if not fill:
        log.error(f"{symbol}: sell order failed — keeping position, will retry next loop")
        return None
    profit = fill["proceeds"] - pos["avg_cost"] * fill["qty"]
    strategy = pos.get("strategy", "dip")
    del state["positions"][symbol]
    state.setdefault("cooldowns", {})[symbol] = now.isoformat()
    strategies.adaptive_update(state["symbol_stats"], symbol, profit >= 0, cfg)
    store.log_trade({"time": now.isoformat(), "action": "SELL", "symbol": symbol,
                     "strategy": strategy, "price": fill["price"], "qty": fill["qty"],
                     "notional": fill["notional"], "fee_usdt": fill["fee_usdt"],
                     "profit": profit, "reason": reason})
    log.info(f"SELL {symbol} @ {fill['price']:.6g} | net profit ${profit:.4f} | {reason}")
    notify.send(f"🔴 SELL {symbol} @ {fill['price']:.6g}\nnet P/L: ${profit:.4f}\n{reason}")
    return profit

def do_partial(ex, store, state, cfg, notify, symbol, fraction, reason, now=None):
    now = now or datetime.utcnow()
    pos = state["positions"].get(symbol)
    if not pos: return None
    qty = ex.round_to_step(pos["qty"] * fraction, ex.lot_step(symbol))
    if qty <= 0:
        pos["partial_taken"] = True   # too small to split — don't retry every loop
        return None
    fill = ex.smart_sell(symbol, qty, cfg)   # partials can afford maker-first
    if not fill: return None
    profit = fill["proceeds"] - pos["avg_cost"] * fill["qty"]
    pos["qty"] = round(pos["qty"] - fill["qty"], 12)
    pos["partial_taken"] = True
    store.log_trade({"time": now.isoformat(), "action": "SELL", "symbol": symbol,
                     "strategy": pos.get("strategy", "dip"), "price": fill["price"],
                     "qty": fill["qty"], "notional": fill["notional"],
                     "fee_usdt": fill["fee_usdt"], "profit": profit,
                     "reason": f"partial ({fraction*100:.0f}%): {reason}"})
    log.info(f"PARTIAL SELL {symbol} @ {fill['price']:.6g} | profit ${profit:.4f} | {reason}")
    notify.send(f"🟡 PARTIAL SELL {symbol} @ {fill['price']:.6g}\nnet P/L: ${profit:.4f}\n{reason}")
    if pos["qty"] <= 1e-12:
        del state["positions"][symbol]
        state.setdefault("cooldowns", {})[symbol] = now.isoformat()
        strategies.adaptive_update(state["symbol_stats"], symbol, profit >= 0, cfg)
    return profit

# ---------------- manual control (dashboard + telegram both use this) ----------------

class Controller:
    def __init__(self, ex, store, state, cfg, notify):
        self.ex, self.store, self.state, self.cfg, self.notify = ex, store, state, cfg, notify

    def close_symbol(self, symbol):
        symbol = (symbol or "").upper().strip()
        if not symbol: return {"ok": False, "msg": "provide a symbol"}
        with state_lock:
            if symbol not in self.state["positions"]:
                return {"ok": False, "msg": f"no open position in {symbol}"}
            try:
                price = float(self.ex.get_symbol_ticker(symbol)["price"])
            except Exception as e:
                return {"ok": False, "msg": f"price fetch failed: {e}"}
            p = do_sell(self.ex, self.store, self.state, self.cfg, self.notify, symbol, "manual close")
            self.store.save_bot_state(self.state)
            ok = p is not None
            return {"ok": ok, "msg": f"closed {symbol} @ {price:.6g}" if ok else f"close failed for {symbol}",
                    "price": price if ok else None}

    def close_all(self):
        with state_lock:
            symbols = list(self.state["positions"].keys())
        if not symbols: return {"ok": True, "msg": "no open positions", "closed": 0}
        closed = sum(1 for s in symbols if self.close_symbol(s)["ok"])
        return {"ok": closed > 0, "closed": closed,
                "msg": f"closed {closed}/{len(symbols)} position(s)"}

    def set_paused(self, val):
        with state_lock:
            self.state["paused"] = bool(val)
            shared["paused"] = bool(val)
            self.store.save_bot_state(self.state)
        log.info(f"Entries {'PAUSED' if val else 'RESUMED'} (open positions still managed)")
        self.notify.send(f"⏸ Entries paused — open positions still managed." if val else "▶ Entries resumed.")
        return {"ok": True, "paused": bool(val),
                "msg": "entries paused" if val else "entries resumed"}

    def status_text(self):
        with state_lock:
            n = len(self.state["positions"])
        t = self.store.profit_summary(); d = self.store.daily_summary(datetime.utcnow().date().isoformat())
        return (f"🤖 Status\nEquity: ${shared.get('equity') or 0:.2f} | Free USDT: ${shared.get('usdt_free') or 0:.2f}\n"
                f"Open: {n}/{self.cfg['MAX_CONCURRENT_POSITIONS']}\n"
                f"Realized: ${t['profit']:.4f} ({t['closed']} trades)\n"
                f"Today: ${d['profit']:.4f}\nLoops: {shared.get('loops', 0)}\n"
                f"Entries: {'PAUSED' if self.state.get('paused') else 'active'}")

    def summary_text(self):
        t = self.store.profit_summary()
        wr = (t['wins']/t['closed']*100) if t['closed'] else 0
        s = self.store.strategy_summary()
        lines = [f"📊 Summary — {t['closed']} closed trades, ${t['profit']:.4f} net, {wr:.0f}% wins"]
        for k, v in s.items():
            lines.append(f"  {k}: {v['closed']} trades, ${v['profit']:.4f}")
        return "\n".join(lines) if s else lines[0]

# ---------------- helpers ----------------

def update_price_history(state, symbol, price):
    hist = state["price_history"].setdefault(symbol, [])
    hist.append(price)
    if len(hist) > CFG["ROLLING_WINDOW"]: hist.pop(0)

def rolling_high(state, symbol):
    hist = state["price_history"].get(symbol, [])
    return max(hist) if hist else None

def mtf_ok(ex, cfg, symbol, price):
    """Higher-timeframe trend confirmation (default 4h MA50): price must be at/above."""
    if not cfg["MTF_ENABLED"]: return True
    raw = ex.get_klines(symbol, cfg["MTF_INTERVAL"], cfg["MTF_MA_PERIOD"] + 2)
    closes = [float(k[4]) for k in raw]
    if len(closes) < cfg["MTF_MA_PERIOD"]: return True   # not enough HTF data — don't block
    return price >= sum(closes[-cfg["MTF_MA_PERIOD"]:]) / cfg["MTF_MA_PERIOD"]

def get_active_symbols(ex, cfg):
    tickers = ex.get_ticker24h()
    if tickers is None: return None
    candidates = []
    for t in tickers:
        sym = t.get("symbol", "")
        if not sym.endswith("USDT") or sym in cfg["MARKET_SCAN_EXCLUDE"]: continue
        try: qv = float(t.get("quoteVolume", 0))
        except (TypeError, ValueError): continue
        if qv < cfg["MARKET_SCAN_MIN_VOLUME_USDT"]: continue
        candidates.append((sym, qv))
    candidates.sort(key=lambda x: x[1], reverse=True)
    top = [s for s, _ in candidates[:cfg["MARKET_SCAN_TOP_N"]]]
    log.info(f"Market scan: {len(candidates)} liquid USDT pairs, watching top {len(top)}")
    return top

def compute_equity(ex, prices):
    acct = ex.get_account()
    if not acct: return None, None
    equity, usdt_free = 0.0, None
    for b in acct.get("balances", []):
        amt = float(b["free"]) + float(b["locked"])
        if amt <= 0: continue
        if b["asset"] == "USDT":
            usdt_free = float(b["free"]); equity += amt
        else:
            p = prices.get(f"{b['asset']}USDT")
            if p: equity += amt * p
    return equity, usdt_free

def reconcile_positions(ex, store, state, cfg):
    """Re-adopt real holdings the bot lost track of (e.g. wiped disk on redeploy).
    Skips testnet faucet coins (no buy-trade history behind the balance)."""
    acct = ex.get_account()
    if not acct: return
    balances = {b["asset"]: float(b["free"]) + float(b["locked"])
                for b in acct.get("balances", []) if float(b["free"]) + float(b["locked"]) > 0}
    prices = ex.get_all_prices() or {}
    recovered = 0
    for asset, amount in balances.items():
        if asset in ("USDT", "BUSD", "USDC", "FDUSD"): continue
        symbol = f"{asset}USDT"
        if symbol in state["positions"]: continue
        price = prices.get(symbol)
        if not price: continue
        qty = ex.round_to_step(amount, ex.lot_step(symbol))
        if qty <= 0 or qty * price < 1.0: continue
        try:
            trades = ex.client.get_my_trades(symbol=symbol, limit=20)
        except Exception:
            continue
        buys = [t for t in trades if t.get("isBuyer")]
        if not buys: continue
        last = buys[-1]
        buy_price = float(last["price"])
        state["positions"][symbol] = {
            "buy_price": buy_price, "qty": qty, "avg_cost": buy_price, "buy_cost": buy_price * qty,
            "timestamp": datetime.utcfromtimestamp(last["time"] / 1000).isoformat(),
            "peak_price": max(buy_price, price), "trailing_active": False,
            "target_pct": cfg["SELL_TARGET_PCT"], "stop_pct": cfg["STOP_LOSS_PCT"],
            "partial_taken": False, "ladder_step": 0, "be_armed": False, "strategy": "dip",
            "trail_activation_pct": cfg["TRAILING_ACTIVATION_PCT"],
            "trail_stop_pct": cfg["TRAILING_STOP_PCT"],
        }
        recovered += 1
        log.info(f"Reconciliation: recovered {symbol} @ {buy_price}, qty {qty}")
    log.info(f"Reconciliation complete: {recovered} position(s) recovered.")

# ---------------- main ----------------

def _connect_with_retry(max_attempts=6):
    """
    The raw Binance Client() constructor calls self.ping() synchronously, which
    can hit a temporary rate-limit ban (code -1003). Without retrying here, that
    crashes the whole process — and on a platform that auto-restarts crashed
    processes, an immediate restart just retries into the SAME still-active ban,
    creating a rapid crash loop that looks like repeated fresh bans in the logs.
    This waits out the ban with exponential backoff instead of crashing.
    """
    delay = 5
    for attempt in range(1, max_attempts + 1):
        try:
            return Client(CFG["API_KEY"], CFG["API_SECRET"], testnet=CFG["USE_TESTNET"])
        except BinanceAPIException as e:
            if attempt == max_attempts:
                log.error(f"Failed to connect to Binance after {max_attempts} attempts: {e}")
                raise
            log.warning(f"Binance connection attempt {attempt}/{max_attempts} failed ({e}); retrying in {delay}s...")
            time.sleep(delay)
            delay = min(delay * 2, 120)  # cap backoff at 2 minutes


def main():
    errors, warns = validate(strict=True)
    for w in warns: log.warning(f"CONFIG WARNING: {w}")
    if errors:
        for e in errors: log.error(f"CONFIG ERROR: {e}")
        raise SystemExit(1)

    # Start the dashboard/health-check server FIRST, before anything that can
    # block for minutes (the Binance connection retry loop below). Fly/Render
    # need to see the port bound quickly or they'll mark the deploy unhealthy —
    # this bit us before when a rate-limit ban meant nothing was listening on
    # the expected port for the whole retry window. init_dashboard() is called
    # again below once store/controller actually exist; the dashboard just
    # shows "connecting..." in the meantime instead of the port being dark.
    shared["status"] = "connecting to Binance..."
    dashboard.init_dashboard(None, None, CFG, shared, {"positions": {}, "trade_log": []},
                              state_lock, _log_buffer, _log_lock)
    threading.Thread(target=dashboard.start_server, args=(CFG["PORT"],), daemon=True).start()
    log.info(f"Dashboard listening on port {CFG['PORT']} (connecting to Binance next, this can take a moment)")

    client = _connect_with_retry()
    ex = Exchange(client, fee_rate=CFG["FEE_RATE"])
    log.info(f"Connected to Binance {'TESTNET' if CFG['USE_TESTNET'] else 'LIVE'}.")

    store = datastore.Store(CFG["STATE_DB"])
    store.migrate_from_state_json("state.json")
    state = store.load_bot_state()

    # backfill fields for positions created by older versions
    for sym, pos in state["positions"].items():
        pos.setdefault("strategy", "dip")
        pos.setdefault("peak_price", pos["buy_price"])
        pos.setdefault("avg_cost", pos.get("buy_price"))
        pos.setdefault("buy_cost", pos.get("avg_cost", pos["buy_price"]) * pos.get("qty", 0))
        pos.setdefault("trailing_active", False)
        pos.setdefault("target_pct", CFG["SELL_TARGET_PCT"])
        pos.setdefault("stop_pct", CFG["STOP_LOSS_PCT"])
        pos.setdefault("partial_taken", False)
        pos.setdefault("ladder_step", 0)
        pos.setdefault("be_armed", False)
        pos.setdefault("trail_activation_pct", CFG["TRAILING_ACTIVATION_PCT"])
        pos.setdefault("trail_stop_pct", CFG["TRAILING_STOP_PCT"])

    # notifications (best effort, never blocks trading)
    telegram = notify_mod.Telegram(CFG["TELEGRAM_BOT_TOKEN"], CFG["TELEGRAM_CHAT_ID"],
                                    controller=None, commands_enabled=False)
    discord = notify_mod.Discord(CFG["DISCORD_WEBHOOK_URL"])
    notify = notify_mod.MultiNotify([telegram, discord])
    controller = Controller(ex, store, state, CFG, notify)
    if CFG["TELEGRAM_BOT_TOKEN"] and CFG["TELEGRAM_CHAT_ID"] and CFG["TELEGRAM_COMMANDS_ENABLED"]:
        telegram.controller = controller
        telegram.commands_enabled = True
    notify.start()

    # Now that store/controller/state are real, wire the dashboard up to the
    # actual running bot instead of the placeholder shown while connecting.
    dashboard.init_dashboard(store, controller, CFG, shared, state, state_lock, _log_buffer, _log_lock)
    shared["status"] = "running"

    # graceful shutdown: Render sends SIGTERM on redeploys
    def _sig(signum, frame):
        log.info(f"Received signal {signum} — shutting down after this step, saving state.")
        _shutdown["flag"] = True
    signal.signal(signal.SIGTERM, _sig)
    signal.signal(signal.SIGINT, _sig)

    log.info("Reconciling against real account balances (can take a minute)...")
    reconcile_positions(ex, store, state, CFG)
    store.save_bot_state(state)

    if os.getenv("FORCE_CLOSE_ALL", "false").lower() == "true" and state["positions"]:
        log.warning("FORCE_CLOSE_ALL set — closing all open positions.")
        controller.close_all()

    log.info(f"Strategies: dip={CFG['STRATEGY_DIP_ENABLED']} bb={CFG['STRATEGY_BB_ENABLED']} "
             f"breakout={CFG['STRATEGY_BREAKOUT_ENABLED']} | maker orders={CFG['MAKER_ORDERS_ENABLED']} "
             f"| MTF={CFG['MTF_ENABLED']} ({CFG['MTF_INTERVAL']} MA{CFG['MTF_MA_PERIOD']})")
    log.info(f"Risk: daily max loss ${CFG['DAILY_MAX_LOSS_USDT']} | max DD {CFG['MAX_DRAWDOWN_PCT']}% | "
             f"risk/trade {CFG['RISK_PER_TRADE_PCT']}% | max exposure {CFG['MAX_TOTAL_EXPOSURE_PCT']}% | "
             f"crash guard {CFG['CRASH_GUARD_ENABLED']} (BTC {CFG['CRASH_GUARD_PCT']}%/1h)")
    log.info(f"Exits: ladder={CFG['LADDER_ENABLED']} {CFG['LADDER_TAKES']} | breakeven={CFG['BREAKEVEN_ENABLED']} "
             f"@ {CFG['BREAKEVEN_TRIGGER_R']}R | trailing={CFG['TRAILING_STOP_ENABLED']}")

    active_symbols = list(CFG["WATCHLIST"])
    if CFG["MARKET_SCAN_ENABLED"]:
        scanned = get_active_symbols(ex, CFG)
        if scanned: active_symbols = scanned
    shared["watchlist"] = active_symbols
    shared["positions_ref_set"] = None  # (positions are read via state under lock)

    notify.send(f"🤖 Bot started ({'TESTNET' if CFG['USE_TESTNET'] else 'LIVE'})\n"
                f"watching {len(active_symbols)} pairs · {len(state['positions'])} open position(s)")

    loop_count = 0
    while not _shutdown["flag"]:
        try:
            if CFG["MARKET_SCAN_ENABLED"] and loop_count > 0 and loop_count % CFG["MARKET_SCAN_REFRESH_LOOPS"] == 0:
                scanned = get_active_symbols(ex, CFG)
                if scanned:
                    active_symbols = scanned
                    shared["watchlist"] = active_symbols

            all_prices = ex.get_all_prices()
            if all_prices is None:
                log.warning("Price fetch failed — backing off one interval.")
                time.sleep(CFG["CHECK_INTERVAL_SEC"]); loop_count += 1; continue

            now = datetime.utcnow()
            today = now.date().isoformat()

            # account equity / drawdown tracking / daily summary — every 5 loops
            if loop_count % 5 == 0:
                try:
                    equity, usdt_free = compute_equity(ex, all_prices)
                    if equity is not None:
                        shared["equity"], shared["usdt_free"] = equity, usdt_free
                        peak = state.get("peak_equity") or equity
                        state["peak_equity"] = max(peak, equity)
                        shared["peak_equity"] = state["peak_equity"]
                except Exception as e:
                    log.error(f"Equity fetch failed: {e}")
                risk.check_crash_guard(ex, CFG, state, now)
                # daily Telegram summary on UTC day rollover
                if state.get("last_summary_day") and state["last_summary_day"] != today:
                    d = store.daily_summary(state["last_summary_day"])
                    if d["closed"]:
                        notify.send(f"📅 {state['last_summary_day']}: {d['closed']} closed trades, "
                                    f"P/L ${d['profit']:.4f}, {d['wins']}/{d['closed']} wins")
                state["last_summary_day"] = today

            daily = store.daily_summary(today)
            shared["paused"] = state.get("paused", False)
            shared["crash_guard_until"] = state.get("crash_guard_until")

            new_entries = 0
            for symbol in active_symbols:
                if _shutdown["flag"]: break
                price = all_prices.get(symbol)
                if price is None: continue
                shared["last_prices"][symbol] = price

                with state_lock:
                    update_price_history(state, symbol, price)
                    if symbol in state["positions"]:
                        raw = ex.get_klines(symbol, CFG["KLINE_INTERVAL"], CFG["INDICATOR_LIMIT"])
                        an = compute_indicators(raw, CFG) if raw else None
                        pos = state["positions"][symbol]
                        act = strategies.evaluate_exits(pos, price, an, now, CFG)
                        if act:
                            if act[0] == "partial":
                                do_partial(ex, store, state, CFG, notify, symbol, act[1], act[2], now)
                            else:
                                do_sell(ex, store, state, CFG, notify, symbol, act[1], now)
                    else:
                        # cheap gate FIRST — skip the klines call entirely when blocked
                        open_notional = sum(p["qty"] * shared["last_prices"].get(s, p["buy_price"])
                                            for s, p in state["positions"].items())

                        # Hard $ cap: if the bot's own open positions already total the
                        # configured budget, don't even bother evaluating this symbol.
                        remaining_budget = CFG["MAX_BOT_CAPITAL_USDT"] - open_notional
                        if remaining_budget <= 0:
                            continue

                        ok, why = risk.entry_gate(CFG, state, daily["profit"], shared.get("equity"),
                                                  len(state["positions"]), symbol, now)
                        if not ok:
                            continue
                        raw = ex.get_klines(symbol, CFG["KLINE_INTERVAL"], CFG["INDICATOR_LIMIT"])
                        an = compute_indicators(raw, CFG) if raw else None
                        ctx = {"symbol": symbol, "price": price, "an": an, "cfg": CFG,
                               "rolling_high": rolling_high(state, symbol),
                               "multiplier": strategies.adaptive_multiplier(state["symbol_stats"], symbol, CFG)}
                        sig = None
                        for fn in strategies.ENTRY_STRATEGIES:
                            sig = fn(ctx)
                            if sig: break
                        if sig and CFG["MTF_ENABLED"] and not mtf_ok(ex, CFG, symbol, price):
                            log.info(f"{symbol}: {sig['strategy']} signal vetoed by {CFG['MTF_INTERVAL']} trend filter")
                            sig = None
                        if sig:
                            quote = risk.size_position(CFG, sig["stop_pct"],
                                                       (an or {}).get("atr_pct"), shared.get("equity"),
                                                       shared.get("usdt_free"), ex.min_notional(symbol))
                            if quote:
                                # Never let a single trade push total deployed capital past the hard cap
                                quote = min(quote, remaining_budget)
                                if quote < ex.min_notional(symbol):
                                    log.info(f"{symbol}: signal skipped — remaining capital (${remaining_budget:.2f}) "
                                             f"below exchange minimum after applying ${CFG['MAX_BOT_CAPITAL_USDT']:.0f} cap")
                                    quote = None
                            if quote and risk.capital_cap_ok(CFG, open_notional, quote) \
                                      and risk.exposure_ok(CFG, shared.get("equity"), open_notional, quote):
                                if do_buy(ex, store, state, CFG, notify, symbol, sig, quote, now):
                                    new_entries += 1
                            elif quote:
                                log.info(f"{symbol}: signal skipped — total exposure cap")

                store.save_bot_state(state)

            loop_count += 1
            shared["loops"] = loop_count
            shared["last_check"] = datetime.utcnow().isoformat()
            log.info(f"Loop {loop_count}: {len(active_symbols)} symbols | entries +{new_entries} | "
                     f"open {len(state['positions'])}/{CFG['MAX_CONCURRENT_POSITIONS']}"
                     + (" | PAUSED" if state.get("paused") else ""))
            if loop_count % 10 == 0:
                t = store.profit_summary()
                log.info(f"--- {t['closed']} closed trades | net ${t['profit']:.4f} | "
                         f"win rate {(t['wins']/t['closed']*100) if t['closed'] else 0:.1f}% ---")
            # sleep in 1s slices so SIGTERM responds quickly
            for _ in range(CFG["CHECK_INTERVAL_SEC"]):
                if _shutdown["flag"]: break
                time.sleep(1)
        except Exception as e:
            # A single bad loop iteration (rate limit, network blip, unexpected API
            # response, etc.) must NEVER crash the whole process — that turned into
            # a real multi-hour outage once before, when an uncaught exception here
            # propagated up, killed the bot, and Fly.io eventually gave up restarting
            # it after hitting its max-restart count. Log it, skip this iteration,
            # and try again next cycle instead.
            log.error(f"Unhandled error in main loop (iteration skipped, will retry next cycle): {e}", exc_info=True)
            loop_count += 1
            for _ in range(CFG["CHECK_INTERVAL_SEC"]):
                if _shutdown["flag"]: break
                time.sleep(1)

    store.save_bot_state(state)
    log.info("Shutdown complete — state saved.")

if __name__ == "__main__":
    main()