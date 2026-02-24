import time
import hmac
import hashlib
try:
    import requests
except Exception:
    requests = None

from urllib.parse import urlencode

BASE_URL = "https://api.binance.com"

# Global time sync cache (shared across client instances)
_GLOBAL_TIME_OFFSET_MS = 0
_GLOBAL_TIME_SYNC_AT = 0.0
_GLOBAL_TIME_SYNC_LOG_AT = 0.0

class BinanceMarginClient:
    def __init__(self, api_key: str, api_secret: str, log_cb):
        self.api_key = api_key
        self.api_secret = api_secret
        self._log = log_cb
        self._time_offset_ms = int(_GLOBAL_TIME_OFFSET_MS)
        self._time_sync_at = float(_GLOBAL_TIME_SYNC_AT)
        self._filters_cache = {}

    def _headers(self):
        return {"X-MBX-APIKEY": self.api_key}

    def _server_time(self):
        if requests is None:
            raise RuntimeError("requests non installato. Esegui: pip install -r requirements.txt")
        r = requests.get(f"{BASE_URL}/api/v3/time", timeout=10)
        r.raise_for_status()
        return r.json()["serverTime"]

    def sync_time(self, force=False):
        global _GLOBAL_TIME_OFFSET_MS, _GLOBAL_TIME_SYNC_AT, _GLOBAL_TIME_SYNC_LOG_AT
        now = time.time()

        try:
            # share cache across instances
            if not force and (now - float(_GLOBAL_TIME_SYNC_AT or 0.0)) < 300.0:
                self._time_offset_ms = int(_GLOBAL_TIME_OFFSET_MS)
                self._time_sync_at = float(_GLOBAL_TIME_SYNC_AT or 0.0)
                return
        except Exception:
            pass

        try:
            st = self._server_time()
            local_ms = int(time.time() * 1000)
            off = int(st) - int(local_ms)

            self._time_offset_ms = int(off)
            self._time_sync_at = float(now)

            _GLOBAL_TIME_OFFSET_MS = int(off)
            _GLOBAL_TIME_SYNC_AT = float(now)

            # avoid log spam
            try:
                if force or (now - float(_GLOBAL_TIME_SYNC_LOG_AT or 0.0)) >= 300.0:
                    _GLOBAL_TIME_SYNC_LOG_AT = float(now)
                    self._log("⏱️ Time sync OK (cache 5m).")
            except Exception:
                pass
        except Exception as e:
            self._log(f"⚠️ Time sync FAIL (fallback local): {e}")

    def _ts(self):
        return int(time.time() * 1000) + int(self._time_offset_ms)

    def _sign(self, params: dict):
        qs = urlencode(params, doseq=True)
        sig = hmac.new(self.api_secret.encode("utf-8"), qs.encode("utf-8"), hashlib.sha256).hexdigest()
        return qs + "&signature=" + sig

    def _signed(self, method: str, path: str, params: dict):
        if requests is None:
            raise RuntimeError("requests non installato. Esegui: pip install -r requirements.txt")
        self.sync_time()
        params = dict(params or {})
        params["timestamp"] = self._ts()
        params["recvWindow"] = 60000

        qs = self._sign(params)
        url = f"{BASE_URL}{path}?{qs}"
        r = requests.request(method, url, headers=self._headers(), timeout=20)
        if r.status_code != 200:
            raise RuntimeError(f"HTTP {r.status_code} {path} -> {r.text}")
        return r.json()

    def ticker_price(self, symbol: str) -> float:
        if requests is None:
            raise RuntimeError("requests non installato. Esegui: pip install -r requirements.txt")
        r = requests.get(f"{BASE_URL}/api/v3/ticker/price", params={"symbol": symbol}, timeout=10)
        r.raise_for_status()
        return float(r.json()["price"])

    def exchange_filters(self, symbol: str) -> dict:
        if requests is None:
            raise RuntimeError("requests non installato. Esegui: pip install -r requirements.txt")
        if symbol in self._filters_cache:
            return self._filters_cache[symbol]
        r = requests.get(f"{BASE_URL}/api/v3/exchangeInfo", params={"symbol": symbol}, timeout=10)
        r.raise_for_status()
        info = r.json()
        s0 = info["symbols"][0]
        out = {}
        for f in s0.get("filters", []):
            out[f["filterType"]] = f
        self._filters_cache[symbol] = out
        return out

    # ---- account state ----
    
    def klines_1d(self, symbol: str, interval: str = "5m", limit: int = 288):
        """
        Public klines (no signature). Default: 24h of 5m candles = 288.
        Returns list of [open_time, open, high, low, close, volume, ...]
        """
        r = requests.get(f"{BASE_URL}/api/v3/klines", params={"symbol": symbol, "interval": interval, "limit": int(limit)}, timeout=12)
        r.raise_for_status()
        return r.json()

    def margin_account_cross(self):
        return self._signed("GET", "/sapi/v1/margin/account", {})

    def margin_account_isolated(self, symbol: str):
        return self._signed("GET", "/sapi/v1/margin/isolated/account", {"symbols": symbol})

    def max_borrowable(self, asset: str, symbol: str, is_isolated: bool) -> float:
        p = {"asset": asset}
        if is_isolated:
            p["isolatedSymbol"] = symbol
        j = self._signed("GET", "/sapi/v1/margin/maxBorrowable", p)
        return float(j.get("amount", "0") or 0)

    # ---- orders ----
    def cancel_open_orders(self, symbol: str, is_isolated: bool):
        p = {"symbol": symbol}
        if is_isolated:
            p["isIsolated"] = "TRUE"
        try:
            self._signed("DELETE", "/sapi/v1/margin/openOrders", p)
            return
        except Exception:
            pass
        try:
            orders = self._signed("GET", "/sapi/v1/margin/openOrders", {"symbol": symbol, "isIsolated": "TRUE" if is_isolated else "FALSE"})
            if isinstance(orders, list):
                for o in orders:
                    oid = o.get("orderId")
                    if not oid:
                        continue
                    try:
                        self._signed("DELETE", "/sapi/v1/margin/order", {"symbol": symbol, "orderId": oid, "isIsolated": "TRUE" if is_isolated else "FALSE"})
                    except:
                        pass
        except:
            pass

    

    def open_orders(self, symbol: str, is_isolated: bool):
        p = {"symbol": symbol}
        if is_isolated:
            p["isIsolated"] = "TRUE"
        else:
            p["isIsolated"] = "FALSE"
        return self._signed("GET", "/sapi/v1/margin/openOrders", p)

    def borrow_repay(self, asset: str, amount: float, action: str, is_isolated: bool, symbol: str):
        """
        action: 'BORROW' o 'REPAY' (usa /sapi/v1/margin/borrow-repay).
        Nota: Binance ha annunciato la dismissione dei vecchi endpoint /loan e /repay,
        quindi usiamo l'endpoint unificato.
        """
        p = {
            "asset": asset,
            "amount": f"{float(amount):.8f}",
            "type": str(action or "").upper()
        }
        if is_isolated:
            p["isIsolated"] = "TRUE"
            p["symbol"] = symbol
        return self._signed("POST", "/sapi/v1/margin/borrow-repay", p)

    def _side_effect_open(self, auto_borrow: bool, auto_repay: bool) -> str:
        if auto_borrow and auto_repay:
            return "AUTO_BORROW_REPAY"
        if auto_borrow:
            return "MARGIN_BUY"
        if auto_repay:
            return "AUTO_REPAY"
        return "NO_SIDE_EFFECT"

    def _side_effect_close(self, auto_repay: bool) -> str:
        return "AUTO_REPAY" if auto_repay else "NO_SIDE_EFFECT"

    def market_buy_quote(self, symbol: str, quote_qty: float, is_isolated: bool, auto_borrow: bool, auto_repay: bool):
        p = {
            "symbol": symbol,
            "side": "BUY",
            "type": "MARKET",
            "quoteOrderQty": f"{quote_qty:.8f}",
            "sideEffectType": self._side_effect_open(auto_borrow, auto_repay),
        }
        if is_isolated:
            p["isIsolated"] = "TRUE"
        return self._signed("POST", "/sapi/v1/margin/order", p)

    def market_buy_qty(self, symbol: str, qty: float, is_isolated: bool, auto_borrow: bool, auto_repay: bool):
        """Market BUY usando quantity (base), come X_Trader_Trading.py."""
        p = {
            "symbol": symbol,
            "side": "BUY",
            "type": "MARKET",
            "quantity": f"{qty:.8f}",
            "sideEffectType": self._side_effect_open(auto_borrow, auto_repay),
        }
        if is_isolated:
            p["isIsolated"] = "TRUE"
        return self._signed("POST", "/sapi/v1/margin/order", p)

    def market_sell_qty(self, symbol: str, qty: float, is_isolated: bool, auto_borrow: bool, auto_repay: bool):
        p = {
            "symbol": symbol,
            "side": "SELL",
            "type": "MARKET",
            "quantity": f"{qty:.8f}",
            "sideEffectType": self._side_effect_open(auto_borrow, auto_repay),
        }
        if is_isolated:
            p["isIsolated"] = "TRUE"
        return self._signed("POST", "/sapi/v1/margin/order", p)

    def close_long_sell(self, symbol: str, qty: float, is_isolated: bool, auto_repay: bool):
        p = {
            "symbol": symbol,
            "side": "SELL",
            "type": "MARKET",
            "quantity": f"{qty:.8f}",
            "sideEffectType": self._side_effect_close(auto_repay),
        }
        if is_isolated:
            p["isIsolated"] = "TRUE"
        return self._signed("POST", "/sapi/v1/margin/order", p)

    def close_short_buy(self, symbol: str, qty: float, is_isolated: bool, auto_repay: bool):
        p = {
            "symbol": symbol,
            "side": "BUY",
            "type": "MARKET",
            "quantity": f"{qty:.8f}",
            "sideEffectType": self._side_effect_close(auto_repay),
        }
        if is_isolated:
            p["isIsolated"] = "TRUE"
        return self._signed("POST", "/sapi/v1/margin/order", p)

    def place_tp_limit(self, symbol: str, side: str, qty: float, price: float, is_isolated: bool, auto_repay: bool):
        p = {
            "symbol": symbol,
            "side": side,
            "type": "LIMIT",
            "timeInForce": "GTC",
            "quantity": f"{qty:.8f}",
            "price": f"{price:.8f}",
            "sideEffectType": self._side_effect_close(auto_repay),
        }
        if is_isolated:
            p["isIsolated"] = "TRUE"
        return self._signed("POST", "/sapi/v1/margin/order", p)

    def place_sl_stop(self, symbol: str, side: str, qty: float, stop_price: float, is_isolated: bool, auto_repay: bool):
        p = {
            "symbol": symbol,
            "side": side,
            "type": "STOP_LOSS",
            "quantity": f"{qty:.8f}",
            "stopPrice": f"{stop_price:.8f}",
            "sideEffectType": self._side_effect_close(auto_repay),
        }
        if is_isolated:
            p["isIsolated"] = "TRUE"
        return self._signed("POST", "/sapi/v1/margin/order", p)

    def place_oco_sl_tp(self, symbol: str, side: str, qty: float, tp_price: float, sl_stop_price: float, sl_limit_price: float, is_isolated: bool, auto_repay: bool):
        """
        Tenta OCO per Margin (TP limit + SL stop-limit), stile script.
        Endpoint: /sapi/v1/margin/order/oco
        """
        p = {
            "symbol": symbol,
            "side": side,
            "quantity": f"{qty:.8f}",
            "price": f"{tp_price:.8f}",
            "stopPrice": f"{sl_stop_price:.8f}",
            "stopLimitPrice": f"{sl_limit_price:.8f}",
            "stopLimitTimeInForce": "GTC",
            "sideEffectType": self._side_effect_close(auto_repay),
        }
        if is_isolated:
            p["isIsolated"] = "TRUE"
        return self._signed("POST", "/sapi/v1/margin/order/oco", p)

    def place_sl_stop_limit(self, symbol: str, side: str, qty: float, sl_price: float, is_isolated: bool, auto_repay: bool):
        p = {
            "symbol": symbol,
            "side": side,
            "type": "STOP_LOSS_LIMIT",
            "timeInForce": "GTC",
            "quantity": f"{qty:.8f}",
            "price": f"{sl_price:.8f}",
            "stopPrice": f"{sl_price:.8f}",
            "sideEffectType": self._side_effect_close(auto_repay),
        }
        if is_isolated:
            p["isIsolated"] = "TRUE"
        return self._signed("POST", "/sapi/v1/margin/order", p)

    def place_tp_take_profit_limit(self, symbol: str, side: str, qty: float, tp_price: float, is_isolated: bool, auto_repay: bool):
        p = {
            "symbol": symbol,
            "side": side,
            "type": "TAKE_PROFIT_LIMIT",
            "timeInForce": "GTC",
            "quantity": f"{qty:.8f}",
            "price": f"{tp_price:.8f}",
            "stopPrice": f"{tp_price:.8f}",
            "sideEffectType": self._side_effect_close(auto_repay),
        }
        if is_isolated:
            p["isIsolated"] = "TRUE"
        return self._signed("POST", "/sapi/v1/margin/order", p)
