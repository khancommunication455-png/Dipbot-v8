"""SQLite persistence. Trades live in a table (queryable, per-strategy analytics
are one GROUP BY); live trading state lives in a key-value table as JSON blobs.
Atomic by nature — no more corrupt state.json if the process dies mid-write."""
import json
import logging
import os
import sqlite3
import threading

log = logging.getLogger("dipbot")

STATE_KEYS = ("positions", "price_history", "cooldowns", "symbol_stats",
              "paused", "peak_equity", "last_summary_day", "crash_guard_until")

class Store:
    def __init__(self, path="bot.db"):
        self.conn = sqlite3.connect(path, check_same_thread=False)
        self.conn.row_factory = sqlite3.Row
        self.lock = threading.RLock()
        with self.lock:
            self.conn.executescript("""
            CREATE TABLE IF NOT EXISTS trades(
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                time TEXT, action TEXT, symbol TEXT, strategy TEXT,
                price REAL, qty REAL, notional REAL, fee_usdt REAL,
                profit REAL, reason TEXT);
            CREATE INDEX IF NOT EXISTS idx_trades_time ON trades(time);
            CREATE TABLE IF NOT EXISTS kv(key TEXT PRIMARY KEY, value TEXT);
            """)
            self.conn.commit()

    def log_trade(self, t):
        with self.lock:
            self.conn.execute(
                "INSERT INTO trades(time,action,symbol,strategy,price,qty,notional,fee_usdt,profit,reason) "
                "VALUES(?,?,?,?,?,?,?,?,?,?)",
                (t.get("time"), t.get("action"), t.get("symbol"), t.get("strategy"),
                 t.get("price"), t.get("qty"), t.get("notional"), t.get("fee_usdt"),
                 t.get("profit"), t.get("reason")))
            self.conn.commit()

    def get_trades(self, limit=None):
        with self.lock:
            if limit:
                rows = self.conn.execute("SELECT * FROM trades ORDER BY id DESC LIMIT ?", (int(limit),)).fetchall()
                return [dict(r) for r in reversed(rows)]
            rows = self.conn.execute("SELECT * FROM trades ORDER BY id").fetchall()
            return [dict(r) for r in rows]

    def profit_summary(self):
        with self.lock:
            r = self.conn.execute(
                "SELECT COUNT(*) n, COALESCE(SUM(profit),0) p, COALESCE(SUM(profit>0),0) w "
                "FROM trades WHERE action='SELL' AND profit IS NOT NULL").fetchone()
        return {"closed": r["n"], "profit": r["p"], "wins": r["w"]}

    def daily_summary(self, day):
        with self.lock:
            r = self.conn.execute(
                "SELECT COUNT(*) n, COALESCE(SUM(profit),0) p, COALESCE(SUM(profit>0),0) w "
                "FROM trades WHERE action='SELL' AND profit IS NOT NULL AND substr(time,1,10)=?",
                (day,)).fetchone()
        return {"closed": r["n"], "profit": r["p"], "wins": r["w"]}

    def strategy_summary(self):
        with self.lock:
            rows = self.conn.execute(
                "SELECT strategy, COUNT(*) n, COALESCE(SUM(profit),0) p, COALESCE(SUM(profit>0),0) w "
                "FROM trades WHERE action='SELL' AND profit IS NOT NULL GROUP BY strategy").fetchall()
        return {r["strategy"]: {"closed": r["n"], "profit": r["p"], "wins": r["w"]} for r in rows}

    # ---------- kv ----------
    def kv_get(self, key, default=None):
        with self.lock:
            row = self.conn.execute("SELECT value FROM kv WHERE key=?", (key,)).fetchone()
        return json.loads(row["value"]) if row else default

    def kv_set(self, key, value):
        with self.lock:
            self.conn.execute(
                "INSERT INTO kv(key,value) VALUES(?,?) "
                "ON CONFLICT(key) DO UPDATE SET value=excluded.value",
                (key, json.dumps(value)))
            self.conn.commit()

    def load_bot_state(self):
        state = {k: ({} if k in ("positions", "price_history", "cooldowns", "symbol_stats") else None)
                 for k in STATE_KEYS}
        state["paused"] = False
        for k in STATE_KEYS:
            v = self.kv_get(k)
            if v is not None: state[k] = v
        return state

    def save_bot_state(self, state):
        for k in STATE_KEYS:
            if k in state: self.kv_set(k, state[k])

    # ---------- one-time migration from the old state.json ----------
    def migrate_from_state_json(self, path="state.json"):
        if self.kv_get("migrated_from_json"): return
        if not os.path.exists(path):
            self.kv_set("migrated_from_json", True); return
        try:
            with open(path) as f:
                old = json.load(f)
        except Exception as e:
            log.warning(f"state.json exists but unreadable ({e}) — skipping migration")
            self.kv_set("migrated_from_json", True); return
        n = 0
        for t in old.get("trade_log", []):
            self.log_trade({"time": t.get("time"), "action": t.get("action"), "symbol": t.get("symbol"),
                            "strategy": t.get("strategy", "dip"), "price": t.get("price"),
                            "qty": None, "notional": None, "fee_usdt": None,
                            "profit": t.get("profit"), "reason": t.get("reason")})
            n += 1
        state = self.load_bot_state()
        state["positions"] = old.get("positions", {})
        state["symbol_stats"] = old.get("symbol_stats", {})
        state["cooldowns"] = old.get("cooldowns", {})
        self.save_bot_state(state)
        self.kv_set("migrated_from_json", True)
        log.info(f"Migrated state.json -> SQLite: {n} trades, {len(state['positions'])} open position(s). "
                 f"The old file was NOT deleted (rename it to keep as backup).")