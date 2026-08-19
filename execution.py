"""Binance API wrapper: retries with backoff, real fill/fee parsing, maker-first
order placement with market fallback, and cached exchange filters."""
import functools
import logging
import math
import time
from binance.exceptions import BinanceAPIException

log = logging.getLogger("dipbot")

def retry_api(max_retries=4):
    """Retry READ-ONLY API calls on rate limits / transient errors with backoff.
    Never applied to order placement (not idempotent)."""
    def deco(fn):
        @functools.wraps(fn)
        def wrapper(self, *a, **k):
            for attempt in range(max_retries + 1):
                try:
                    return fn(self, *a, **k)
                except BinanceAPIException as e:
                    code = getattr(e, "code", None) or 0
                    if code in (429, 418, -1003) and attempt < max_retries:
                        wait = (2 ** attempt) * 3
                        log.warning(f"{fn.__name__} rate-limited (code {code}); retry {attempt+1}/{max_retries} in {wait}s")
                        time.sleep(wait); continue
                    raise
                except Exception:
                    if attempt < max_retries:
                        time.sleep(2 ** attempt); continue
                    raise
            return None
        return wrapper
    return deco

def _decimals(step):
    s = f"{step:.10f}".rstrip("0")
    return len(s.split(".")[1]) if "." in s else 0

class Exchange:
    def __init__(self, client, fee_rate=0.001):
        self.client = client
        self.fee_rate = fee_rate
        self._lot, self._tick, self._minnot = {}, {}, {}

    # ---------- data (retry-safe) ----------
    @retry_api()
    def get_all_prices(self):
        return {t["symbol"]: float(t["price"]) for t in self.client.get_all_tickers()}

    @retry_api()
    def get_klines(self, symbol, interval, limit):
        return self.client.get_klines(symbol=symbol, interval=interval, limit=limit) or []

    @retry_api()
    def get_ticker24h(self):
        return self.client.get_ticker() or []

    @retry_api()
    def get_symbol_ticker(self, symbol):
        return self.client.get_symbol_ticker(symbol=symbol)

    @retry_api()
    def get_account(self):
        return self.client.get_account()

    # ---------- exchange filters (cached) ----------
    def _info(self, symbol):
        try:
            return self.client.get_symbol_info(symbol) or {}
        except BinanceAPIException as e:
            log.error(f"get_symbol_info failed for {symbol}: {e}")
            return {}

    def lot_step(self, symbol):
        if symbol not in self._lot:
            step = 0.000001
            for f in self._info(symbol).get("filters", []):
                if f.get("filterType") == "LOT_SIZE":
                    step = float(f["stepSize"]); break
            self._lot[symbol] = step
        return self._lot[symbol]

    def price_tick(self, symbol):
        if symbol not in self._tick:
            tick = 0.001
            for f in self._info(symbol).get("filters", []):
                if f.get("filterType") == "PRICE_FILTER":
                    tick = float(f["tickSize"]); break
            self._tick[symbol] = tick
        return self._tick[symbol]

    def min_notional(self, symbol):
        if symbol not in self._minnot:
            mn = 5.0
            for f in self._info(symbol).get("filters", []):
                if f.get("filterType") in ("NOTIONAL", "MIN_NOTIONAL"):
                    mn = float(f.get("minNotional", 5)); break
            self._minnot[symbol] = mn
        return self._minnot[symbol]

    def round_to_step(self, quantity, step):
        if step <= 0: return quantity
        result = int(quantity / step) * step
        return round(result, _decimals(step))

    # ---------- fills ----------
    def _fill(self, order, side):
        """Normalize an order response into {price, qty, notional, fee_usdt, cost|proceeds}
        using the ACTUAL fills — no more recording the pre-trade ticker as the price."""
        fills = order.get("fills") or []
        base = order["symbol"][:-4] if order.get("symbol", "").endswith("USDT") else ""
        if fills:
            qty = sum(float(f["qty"]) for f in fills)
            notional = sum(float(f["qty"]) * float(f["price"]) for f in fills)
            fee_usdt, fee_base = 0.0, 0.0
            for f in fills:
                c, a = float(f["commission"]), f.get("commissionAsset", "")
                if a == "USDT": fee_usdt += c
                elif base and a == base: fee_base += c
                else: fee_usdt += c * float(f["price"])  # BNB etc, valued at fill price
        else:  # some responses (esp. testnet) omit fills — fall back to executed totals
            qty = float(order.get("executedQty") or 0)
            notional = float(order.get("cummulativeQuoteQty") or 0)
            fee_usdt, fee_base = notional * self.fee_rate, 0.0
        if qty <= 0: return None
        out = {"price": notional / qty, "qty": qty, "notional": notional, "fee_usdt": fee_usdt}
        if side == "buy":
            out["qty"] = qty - fee_base          # fees paid in the coin reduce what we hold
            out["cost"] = notional + fee_usdt
        else:
            out["proceeds"] = notional - fee_usdt
        return out

    def _merge(self, *fills):
        fs = [f for f in fills if f]
        if not fs: return None
        if len(fs) == 1: return fs[0]
        qty = sum(f["qty"] for f in fs); notional = sum(f["notional"] for f in fs)
        fee = sum(f["fee_usdt"] for f in fs)
        out = {"qty": qty, "notional": notional, "fee_usdt": fee, "price": notional / qty if qty else 0}
        if "cost" in fs[0]: out["cost"] = sum(f.get("cost", 0) for f in fs)
        if "proceeds" in fs[0]: out["proceeds"] = sum(f.get("proceeds", 0) for f in fs)
        return out

    # ---------- orders (NO auto-retry: not idempotent) ----------
    def market_buy(self, symbol, quote_qty):
        try:
            order = self.client.order_market_buy(symbol=symbol, quoteOrderQty=round(quote_qty, 2))
            return self._fill(order, "buy")
        except BinanceAPIException as e:
            log.error(f"Market buy failed for {symbol}: {e}")
            return None

    def market_sell(self, symbol, qty):
        try:
            order = self.client.order_market_sell(symbol=symbol, quantity=qty)
            return self._fill(order, "sell")
        except BinanceAPIException as e:
            log.error(f"Market sell failed for {symbol} (qty {qty}): {e}")
            return None

    def maker_buy(self, symbol, quote_qty, wait_sec):
        """Limit-buy at the current bid, wait, cancel remainder + market-fill the rest.
        Captures the maker fee when it fills; degrades to taker when it doesn't."""
        try:
            book = self.client.get_order_book(symbol=symbol, limit=5)
            bid = float(book["bids"][0][0])
        except Exception as e:
            log.warning(f"{symbol}: order book fetch failed ({e}) — falling back to market buy")
            return self.market_buy(symbol, quote_qty)
        tick, step = self.price_tick(symbol), self.lot_step(symbol)
        px = math.floor(bid / tick) * tick
        qty = self.round_to_step(quote_qty / px, step)
        if qty <= 0 or qty * px < self.min_notional(symbol):
            return self.market_buy(symbol, quote_qty)
        try:
            order = self.client.order_limit_buy(symbol=symbol,
                                                quantity=f"{qty:.{_decimals(step)}f}",
                                                price=f"{px:.{_decimals(tick)}f}")
            time.sleep(wait_sec)
            order = self.client.get_order(symbol=symbol, orderId=order["orderId"])
            if order["status"] == "FILLED":
                return self._fill(order, "buy")
            try:
                self.client.cancel_order(symbol=symbol, orderId=order["orderId"])
            except BinanceAPIException:
                pass
            order = self.client.get_order(symbol=symbol, orderId=order["orderId"])
            f1 = self._fill(order, "buy") if float(order.get("executedQty") or 0) > 0 else None
            spent = (f1["notional"] + f1["fee_usdt"]) if f1 else 0.0
            remaining = quote_qty - spent
            f2 = self.market_buy(symbol, remaining) if remaining >= self.min_notional(symbol) else None
            return self._merge(f1, f2)
        except BinanceAPIException as e:
            log.error(f"Maker buy failed for {symbol}: {e} — falling back to market")
            return self.market_buy(symbol, quote_qty)

    def maker_sell(self, symbol, qty, wait_sec):
        try:
            book = self.client.get_order_book(symbol=symbol, limit=5)
            ask = float(book["asks"][0][0])
        except Exception as e:
            log.warning(f"{symbol}: order book fetch failed ({e}) — falling back to market sell")
            return self.market_sell(symbol, qty)
        tick, step = self.price_tick(symbol), self.lot_step(symbol)
        px = math.floor(ask / tick) * tick
        try:
            order = self.client.order_limit_sell(symbol=symbol,
                                                 quantity=f"{qty:.{_decimals(step)}f}",
                                                 price=f"{px:.{_decimals(tick)}f}")
            time.sleep(wait_sec)
            order = self.client.get_order(symbol=symbol, orderId=order["orderId"])
            if order["status"] == "FILLED":
                return self._fill(order, "sell")
            try:
                self.client.cancel_order(symbol=symbol, orderId=order["orderId"])
            except BinanceAPIException:
                pass
            order = self.client.get_order(symbol=symbol, orderId=order["orderId"])
            if float(order.get("executedQty") or 0) >= qty:
                return self._fill(order, "sell")
            done = float(order.get("executedQty") or 0)
            f1 = self._fill(order, "sell") if done > 0 else None
            remaining = self.round_to_step(qty - done, step)
            f2 = self.market_sell(symbol, remaining) if remaining > 0 else None
            return self._merge(f1, f2)
        except BinanceAPIException as e:
            log.error(f"Maker sell failed for {symbol}: {e} — falling back to market")
            return self.market_sell(symbol, qty)

    def smart_buy(self, symbol, quote_qty, cfg):
        if cfg["MAKER_ORDERS_ENABLED"] and cfg["MAKER_WAIT_SEC"] > 0:
            return self.maker_buy(symbol, quote_qty, cfg["MAKER_WAIT_SEC"])
        return self.market_buy(symbol, quote_qty)

    def smart_sell(self, symbol, qty, cfg):
        if cfg["MAKER_ORDERS_ENABLED"] and cfg["MAKER_WAIT_SEC"] > 0:
            return self.maker_sell(symbol, qty, cfg["MAKER_WAIT_SEC"])
        return self.market_sell(symbol, qty)