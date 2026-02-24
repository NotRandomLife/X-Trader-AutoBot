import time
import threading
import queue
from decimal import Decimal, ROUND_DOWN, ROUND_UP
from datetime import datetime, timezone, timedelta

from binance_margin import BinanceMarginClient
from emailer import EmailSender


def _parse_symbol_parts(symbol: str):
    known = ["USDT", "USDC", "BUSD", "FDUSD", "TUSD", "EUR", "BTC", "ETH"]
    for q in known:
        if symbol.endswith(q):
            return symbol[:-len(q)], q
    return symbol[:-4], symbol[-4:]


def _floor_to_step(value: float, step: float) -> float:
    if step <= 0:
        return value
    n = int(value / step)
    return n * step


def _quantize_to_step(value: float, step: float, rounding) -> float:
    try:
        if step <= 0:
            return float(value)
        v = Decimal(str(value))
        s = Decimal(str(step))
        q = (v / s).to_integral_value(rounding=rounding)
        out = q * s
        return float(out)
    except Exception:
        return float(value)



def _pct(v):
    """
    Normalizza percentuali come nello script X_Trader_Trading.py:
      - stringhe con '%' -> divide per 100
      - |x| >= 1 -> divide per 100 (es. 1.2 -> 0.012)
      - 0.5 <= |x| < 1 -> considera come percento e divide per 100 (es. 0.8 -> 0.008)
      - |x| < 0.5 -> considera già frazione (es. 0.008 resta 0.008)
    Ritorna una FRAZIONE (0.008 = 0.8%).
    """
    try:
        if isinstance(v, str) and v.strip().endswith("%"):
            v = v.strip().replace("%", "")
        x = float(v)
        ax = abs(x)
        if ax >= 1.0:
            return x / 100.0
        if 0.5 <= ax < 1.0:
            return x / 100.0
        return x
    except Exception:
        return 0.0


class TraderEngine:
    """
    Behaviour:
    - Opera SOLO se trading enabled + sito connesso (pagina bridge/sito aperta).
    - Prefetch maxBorrowable 10s prima del boundary 5m.
    - Dimensionamento: usa TUTTO (free + maxBorrowable_safety) — nessuna % conto.
    - Non apre posizioni se già aperta (controllo SOLO su debito: borrowedQuote=LONG, borrowedBase=SHORT).
    - maxBorrowable applica safety: -leverage% (come X_Trader_Trading.py: leverage = safety %).
    """
    def __init__(self, settings_getter, log_cb, signal_queue: queue.Queue, site_connected_getter):
        self._get_settings = settings_getter
        self._log = log_cb
        self._q = signal_queue
        self._site_connected = site_connected_getter

        self._thr = None
        self._stop = threading.Event()
        self._enabled = False

        self._armed_at = None
        self._last_signal_at = None

        self._cached = {"symbol": None, "base": None, "quote": None, "max_base": 0.0, "max_quote": 0.0}
        self._next_sync = None
        self._did_prefetch_for = None

        self._pos_monitor_next = 0.0
        self._pos_last = {"symbol": None, "pos": "unknown", "debt_total": 0.0, "borrowed_base": 0.0, "borrowed_quote": 0.0}
        self._guard_next = 0.0
        self._last_entry = {"symbol": None, "side": None, "entry": 0.0, "qty": 0.0}

        self._state_lock = threading.Lock()
        self._ui_state = {
            "decision": "-",
            "position": "unknown",
            "last_action": "-",
            "symbol": None,
            "borrowed_base": 0.0,
            "borrowed_quote": 0.0,
            "free_base": 0.0,
            "free_quote": 0.0,
            "have_sl": False,
            "have_tp": False,
            "updated": "",
            "price": 0.0,
            "est_equity_quote": 0.0,
        }

    def start(self):
        if self._thr and self._thr.is_alive():
            return
        self._thr = threading.Thread(target=self._run, daemon=True)
        self._thr.start()

    def stop(self):
        self._stop.set()

    def enable_trading(self, enabled: bool):
        self._enabled = bool(enabled)
        if self._enabled:
            self._armed_at = datetime.now(timezone.utc)
            try:
                with self._state_lock:
                    self._ui_state["last_action"] = "ARMED"
            except Exception:
                pass
            self._log("🛡️ Armato: ignoro segnali precedenti, attendo il prossimo.")
        else:
            try:
                with self._state_lock:
                    self._ui_state["last_action"] = "STOP"
            except Exception:
                pass
            self._log("🛑 Disarmato.")

    def get_ui_state(self):
        try:
            with self._state_lock:
                return dict(self._ui_state)
        except Exception:
            return {}

    def _compute_next_5m_boundary(self, now_utc: datetime):
        m = now_utc.minute
        add = (5 - (m % 5)) % 5
        if add == 0 and (now_utc.second > 0 or now_utc.microsecond > 0):
            add = 5
        return (now_utc.replace(second=0, microsecond=0) + timedelta(minutes=add))

    def _run(self):
        while not self._stop.is_set():
            try:
                self._tick_scheduler()
                self._tick_position_monitor()
                self._tick_signals()
            except Exception as e:
                self._log(f"❌ Engine error: {e}")
            time.sleep(0.2)

    def _tick_scheduler(self):
        if not self._enabled:
            return
        if not self._site_connected():
            return

        s = self._get_settings()
        symbol = (s.get("symbol") or "BTCUSDC").upper()

        now = datetime.now(timezone.utc)
        nxt = self._compute_next_5m_boundary(now)
        if self._next_sync is None or nxt != self._next_sync:
            self._next_sync = nxt
            self._did_prefetch_for = None

        prefetch_at = self._next_sync - timedelta(seconds=10)
        if now >= prefetch_at and self._did_prefetch_for != self._next_sync:
            self._did_prefetch_for = self._next_sync
            self._prefetch_maxborrowable_and_sync(symbol)

    def _safety_factor(self):
        s = self._get_settings()
        try:
            leverage = float(s.get("leverage", 0) or 0.0)
        except:
            leverage = 0.0
        if leverage < 0:
            leverage = 0.0
        if leverage > 99:
            leverage = 99.0
        safety_factor = 1.0 - (leverage / 100.0)
        if safety_factor < 0.01:
            safety_factor = 0.01
        return safety_factor, leverage

    def _prefetch_maxborrowable_and_sync(self, symbol: str):
        s = self._get_settings()
        api_key = s.get("api_key", "")
        api_secret = s.get("api_secret", "")
        if not api_key or not api_secret:
            self._log("⚠️ Prefetch: API/Secret mancanti.")
            return

        base, quote = _parse_symbol_parts(symbol)
        is_isolated = (s.get("margin_mode", "isolated") == "isolated")

        safety_factor, leverage = self._safety_factor()

        cli = BinanceMarginClient(api_key, api_secret, self._log)
        cli.sync_time()
        try:
            max_q = cli.max_borrowable(quote, symbol, is_isolated) * safety_factor
            max_b = cli.max_borrowable(base, symbol, is_isolated) * safety_factor
            self._cached.update({"symbol": symbol, "base": base, "quote": quote, "max_base": float(max_b), "max_quote": float(max_q)})
            self._log(f"📦 Prefetch maxBorrowable OK (safety -{leverage:.2f}%) — {base}:{max_b:.6f}  {quote}:{max_q:.2f}")
            try:
                px = cli.ticker_price(symbol)
            except Exception:
                px = 0.0
            self._log_portfolio_snapshot(cli, symbol, is_isolated, base, quote, px, tag="Prefetch")
        except Exception as e:
            self._log(f"❌ Prefetch maxBorrowable ERR — {e}")


    def _tick_position_monitor(self):
        """
        Background monitor (stile X_Trader_Trading.py):
        - Stato posizione SOLO da DEBT: borrowedQuote=LONG, borrowedBase=SHORT, no debt=FLAT.
        - Se la posizione si chiude in background (SL/TP/manuale) => log + cancella ordini residui.
        - Se resta debito residuo (molto ridotto) e non ci sono open orders => prova REPAY.
        - Guard SL/TP: se la posizione è aperta ma manca SL/TP => riposiziona (cooldown).
        """
        if not self._enabled:
            return
        if not self._site_connected():
            return

        now_ts = time.time()
        if now_ts < float(self._pos_monitor_next or 0.0):
            return
        self._pos_monitor_next = now_ts + 3.0

        s = self._get_settings()
        api_key = s.get("api_key", "")
        api_secret = s.get("api_secret", "")
        if not api_key or not api_secret:
            return

        symbol = (s.get("symbol") or "BTCUSDC").upper()
        base, quote = _parse_symbol_parts(symbol)
        is_isolated = (s.get("margin_mode", "isolated") == "isolated")
        auto_repay = bool(s.get("auto_repay", True))

        sl_pct = _pct(s.get("sl_pct", 0))
        tp_pct = _pct(s.get("tp_pct", 0))

        cli = BinanceMarginClient(api_key, api_secret, self._log)
        try:
            cli.sync_time()
        except Exception:
            pass

        try:
            price = cli.ticker_price(symbol)
        except Exception:
            price = 0.0

        try:
            pos, borrowed_base, borrowed_quote, free_base, free_quote = self._position_by_debt(cli, symbol, is_isolated, base, quote)
        except Exception:
            return

        debt_total = float(borrowed_base) + float(borrowed_quote)

        last_symbol = self._pos_last.get("symbol")
        last_pos = self._pos_last.get("pos", "unknown")
        try:
            last_total = float(self._pos_last.get("debt_total", 0.0) or 0.0)
        except Exception:
            last_total = 0.0

        # ✅ Riconoscimento chiusura in background (DEBT->0)
        if last_symbol == symbol:
            if last_pos in ("long", "short") and pos == "flat":
                try:
                    with self._state_lock:
                        self._ui_state["last_action"] = "CLOSE (BG)"
                except Exception:
                    pass
                self._log(f"✅ POS CHIUSA (DEBT=0) — era {last_pos.upper()} (SL/TP o manuale)")
                try:
                    self._log_portfolio_snapshot(cli, symbol, is_isolated, base, quote, price, tag="AfterCloseBG")
                except Exception:
                    pass
                try:
                    cli.cancel_open_orders(symbol, is_isolated)
                except Exception:
                    pass
                self._last_entry = {"symbol": None, "side": None, "entry": 0.0, "qty": 0.0}

            elif last_pos == "flat" and pos in ("long", "short") and debt_total > 0.0:
                # Posizione aperta senza passare da questo bot (manuale/esterno)
                self._log(f"📌 POS APERTA da DEBT (background) — {pos.upper()} | debt: {base} {borrowed_base:.6f} {quote} {borrowed_quote:.2f}")

        # 🔍 Debito residuo dopo chiusura in background (stile script)
        try:
            if debt_total > 0.0 and last_total > 0.0 and debt_total < (last_total * 0.2):
                try:
                    open_orders = cli.open_orders(symbol, is_isolated)
                except Exception:
                    open_orders = []
                if not open_orders:
                    self._log(f"⚠️ Debito residuo rilevato — prima≈{last_total:.8f} ora≈{debt_total:.8f} | provo REPAY")
                    if auto_repay:
                        try:
                            if float(borrowed_base) > 0.0:
                                cli.borrow_repay(base, borrowed_base, "REPAY", is_isolated, symbol)
                        except Exception:
                            pass
                        try:
                            if float(borrowed_quote) > 0.0:
                                cli.borrow_repay(quote, borrowed_quote, "REPAY", is_isolated, symbol)
                        except Exception:
                            pass

                        time.sleep(0.3)
                        try:
                            pos2, bb2, bq2, fb2, fq2 = self._position_by_debt(cli, symbol, is_isolated, base, quote)
                            debt2 = float(bb2) + float(bq2)
                            if debt2 <= 0.0:
                                self._log("✅ Debito residuo saldato. Riparto flat.")
                            else:
                                self._log(f"⚠️ Debito residuo ancora presente: {debt2:.8f}")
                        except Exception:
                            pass
        except Exception:
            pass

        # 🛡️ Guard SL/TP: se posizione aperta ma manca SL/TP => riposiziona (cooldown)
        try:
            if pos in ("long", "short") and (sl_pct > 0.0 or tp_pct > 0.0):
                if now_ts >= float(self._guard_next or 0.0):
                    self._guard_next = now_ts + 12.0

                    try:
                        open_orders = cli.open_orders(symbol, is_isolated)
                    except Exception:
                        open_orders = []

                    want_side = "SELL" if pos == "long" else "BUY"
                    have_sl = False
                    have_tp = False

                    if isinstance(open_orders, list):
                        for o in open_orders:
                            if not isinstance(o, dict):
                                continue
                            side = str(o.get("side", "")).upper()
                            if side != want_side:
                                continue
                            typ = str(o.get("type", "")).upper()
                            if typ.startswith("STOP_LOSS"):
                                have_sl = True
                            if typ == "LIMIT" or typ == "LIMIT_MAKER" or typ.startswith("TAKE_PROFIT"):
                                have_tp = True

                    try:
                        with self._state_lock:
                            self._ui_state["have_sl"] = bool(have_sl)
                            self._ui_state["have_tp"] = bool(have_tp)
                    except Exception:
                        pass

                    qty = 0.0
                    try:
                        qty = float(self._last_entry.get("qty", 0.0) or 0.0)
                    except Exception:
                        qty = 0.0

                    if qty <= 0.0:
                        if pos == "long":
                            qty = float(free_base) * 0.999
                        else:
                            qty = float(borrowed_base) * 1.001 if float(borrowed_base) > 0.0 else float(free_base) * 0.999

                    entry = 0.0
                    try:
                        entry = float(self._last_entry.get("entry", 0.0) or 0.0)
                    except Exception:
                        entry = 0.0
                    if entry <= 0.0:
                        entry = float(self._last_entry.get("entry", price) or price or 0.0)

                    if qty > 0.0 and entry > 0.0:
                        filters = cli.exchange_filters(symbol)
                        lot = filters.get("LOT_SIZE") or filters.get("MARKET_LOT_SIZE") or {}
                        step = float(lot.get("stepSize", "0") or 0)
                        pf = filters.get("PRICE_FILTER") or {}
                        tick = float(pf.get("tickSize", "0") or 0)

                        if step > 0:
                            qty = _floor_to_step(qty, step)

                        if sl_pct > 0.0 and not have_sl:
                            if pos == "long":
                                sp = entry * (1 - sl_pct)
                                sp = _quantize_to_step(sp, tick, ROUND_DOWN)
                            else:
                                sp = entry * (1 + sl_pct)
                                sp = _quantize_to_step(sp, tick, ROUND_UP)
                            self._log("🛡️ SL mancante → riposiziono")
                            cli.place_sl_stop_limit(symbol, want_side, qty, sp, is_isolated, auto_repay)

                        if tp_pct > 0.0 and not have_tp:
                            if pos == "long":
                                tp = entry * (1 + tp_pct)
                                tp = _quantize_to_step(tp, tick, ROUND_DOWN)
                            else:
                                tp = entry * (1 - tp_pct)
                                tp = _quantize_to_step(tp, tick, ROUND_UP)
                            self._log("🎯 TP mancante → riposiziono")
                            cli.place_tp_take_profit_limit(symbol, want_side, qty, tp, is_isolated, auto_repay)
        except Exception:
            pass

        
        # update ui state (position/portfolio) — thread-safe snapshot
        try:
            est_eq = 0.0
            if float(price or 0.0) > 0.0:
                est_eq = float(free_quote) + (float(free_base) * float(price)) - float(borrowed_quote) - (float(borrowed_base) * float(price))
            with self._state_lock:
                self._ui_state["position"] = str(pos)
                self._ui_state["symbol"] = symbol
                self._ui_state["borrowed_base"] = float(borrowed_base)
                self._ui_state["borrowed_quote"] = float(borrowed_quote)
                self._ui_state["free_base"] = float(free_base)
                self._ui_state["free_quote"] = float(free_quote)
                self._ui_state["price"] = float(price or 0.0)
                self._ui_state["est_equity_quote"] = float(est_eq)
        except Exception:
            pass

# update last state
        self._pos_last = {
            "symbol": symbol,
            "pos": pos,
            "debt_total": float(debt_total),
            "borrowed_base": float(borrowed_base),
            "borrowed_quote": float(borrowed_quote),
        }
    def _tick_signals(self):
        try:
            payload = self._q.get_nowait()
        except queue.Empty:
            return

        if not isinstance(payload, dict):
            return

        # robust key lookup (case-insensitive)
        sig_v = payload.get("signal", None)
        if sig_v is None:
            for k in payload.keys():
                if str(k).lower() == "signal":
                    sig_v = payload.get(k)
                    break
        sig = str(sig_v if sig_v is not None else "hold").lower().strip()

        at_s = payload.get("at") or payload.get("timestamp_utc") or payload.get("ts") or payload.get("timestamp") or ""
        if not at_s:
            for k in payload.keys():
                kl = str(k).lower()
                if kl in ("at", "timestamp_utc", "ts", "timestamp"):
                    at_s = payload.get(k)
                    break

        pair = payload.get("pair") or payload.get("symbol") or self._get_settings().get("symbol") or "BTCUSDC"
        if not pair:
            for k in payload.keys():
                kl = str(k).lower()
                if kl in ("pair", "symbol"):
                    pair = payload.get(k)
                    break
        pair = str(pair).upper().strip()

        if not self._enabled:
            self._log("⏭️ Ignorato: STATO STOP.")
            return

        if not self._site_connected():
            self._log("⏭️ Ignorato: SITO NON CONNESSO.")
            return

        # Parse timestamp
        at_dt = None
        try:
            if isinstance(at_s, (int, float)):
                ts = float(at_s)
                if ts > 1e12:
                    ts = ts / 1000.0
                at_dt = datetime.fromtimestamp(ts, tz=timezone.utc)
            else:
                s_at = str(at_s).strip()
                if s_at.isdigit():
                    ts = float(s_at)
                    if ts > 1e12:
                        ts = ts / 1000.0
                    at_dt = datetime.fromtimestamp(ts, tz=timezone.utc)
                else:
                    at_dt = datetime.fromisoformat(s_at.replace("Z", "+00:00"))
        except:
            at_dt = None

        if not at_dt:
            at_dt = datetime.now(timezone.utc)
        if at_dt.tzinfo is None:
            at_dt = at_dt.replace(tzinfo=timezone.utc)

        if self._armed_at and at_dt <= self._armed_at:
            self._log(f"⏭️ Ignorato: pre-ARM (signal={sig.upper()} at={at_dt.isoformat()}).")
            return

        if self._last_signal_at and str(at_s) == str(self._last_signal_at):
            self._log(f"⏭️ Ignorato: duplicato at={at_s}.")
            return
        self._last_signal_at = at_s

        if sig == "hold":
            try:
                with self._state_lock:
                    self._ui_state["decision"] = "HOLD"
                    self._ui_state["last_action"] = "NO ACTION"
                    self._ui_state["updated"] = str(at_s)
                    self._ui_state["symbol"] = pair
            except Exception:
                pass
            self._log(f"🟡 HOLD {pair} ({at_s}) — no action")
            return

        if sig not in ("buy", "sell"):
            self._log(f"⚠️ Signal sconosciuto '{sig}' ({at_s}) — ignorato")
            return

        try:
            with self._state_lock:
                self._ui_state["decision"] = sig.upper()
                self._ui_state["updated"] = str(at_s)
                self._ui_state["symbol"] = pair
        except Exception:
            pass

        self._log(f"📥 Signal RX — {sig.upper()} {pair} ({at_s})")
        self._execute_trade(pair, sig)

    def _position_by_debt(self, cli: BinanceMarginClient, symbol: str, is_isolated: bool, base: str, quote: str):
        borrowed_base = 0.0
        borrowed_quote = 0.0
        free_base = 0.0
        free_quote = 0.0

        if is_isolated:
            iso = cli.margin_account_isolated(symbol)
            assets = iso.get("assets") or iso.get("data") or []
            row = None
            for a in assets:
                if str(a.get("symbol", "")).upper() == symbol:
                    row = a
                    break
            if not row and assets:
                row = assets[0]

            if row:
                b = row.get("baseAsset") or {}
                q = row.get("quoteAsset") or {}
                borrowed_base = float(b.get("borrowed", 0) or 0) + float(b.get("interest", 0) or 0)
                borrowed_quote = float(q.get("borrowed", 0) or 0) + float(q.get("interest", 0) or 0)
                free_base = float(b.get("free", 0) or 0) + float(b.get("locked", 0) or 0)
                free_quote = float(q.get("free", 0) or 0) + float(q.get("locked", 0) or 0)
        else:
            cross = cli.margin_account_cross()
            ua = cross.get("userAssets") or []
            for a in ua:
                if str(a.get("asset", "")).upper() == base:
                    borrowed_base = float(a.get("borrowed", 0) or 0) + float(a.get("interest", 0) or 0)
                    free_base = float(a.get("free", 0) or 0) + float(a.get("locked", 0) or 0)
                if str(a.get("asset", "")).upper() == quote:
                    borrowed_quote = float(a.get("borrowed", 0) or 0) + float(a.get("interest", 0) or 0)
                    free_quote = float(a.get("free", 0) or 0) + float(a.get("locked", 0) or 0)

        if borrowed_quote > 0:
            pos = "long"
        elif borrowed_base > 0:
            pos = "short"
        else:
            pos = "flat"

        return pos, borrowed_base, borrowed_quote, free_base, free_quote

    def _ensure_cached(self, cli: BinanceMarginClient, symbol: str, is_isolated: bool):
        # On-demand fetch if prefetch not executed yet
        if self._cached.get("symbol") == symbol and (self._cached.get("max_base", 0) or 0) > 0:
            return
        base, quote = _parse_symbol_parts(symbol)
        safety_factor, leverage = self._safety_factor()
        try:
            max_q = cli.max_borrowable(quote, symbol, is_isolated) * safety_factor
            max_b = cli.max_borrowable(base, symbol, is_isolated) * safety_factor
            self._cached.update({"symbol": symbol, "base": base, "quote": quote, "max_base": float(max_b), "max_quote": float(max_q)})
            self._log(f"📦 maxBorrowable on-demand (safety -{leverage:.2f}%) — {base}:{max_b:.6f}  {quote}:{max_q:.2f}")
        except Exception as e:
            self._log(f"⚠️ maxBorrowable on-demand FAIL — {e}")


    def _log_portfolio_snapshot(self, cli: BinanceMarginClient, symbol: str, is_isolated: bool, base: str, quote: str, price: float = 0.0, tag: str = ""):
        try:
            pos, borrowed_base, borrowed_quote, free_base, free_quote = self._position_by_debt(cli, symbol, is_isolated, base, quote)
            if not price or price <= 0:
                try:
                    price = cli.ticker_price(symbol)
                except Exception:
                    price = 0.0

            est_eq = None
            if price and price > 0:
                est_eq = (free_quote - borrowed_quote) + (free_base - borrowed_base) * price

            t = (tag + " ").strip()
            if est_eq is None:
                self._log(
                    f"📊 {t}Portfolio {symbol} — pos={pos.upper()} | free: {base} {free_base:.6f} {quote} {free_quote:.2f} | debt: {base} {borrowed_base:.6f} {quote} {borrowed_quote:.2f}"
                )
            else:
                self._log(
                    f"📊 {t}Portfolio {symbol} — pos={pos.upper()} price={price:.8f} | free: {base} {free_base:.6f} {quote} {free_quote:.2f} | debt: {base} {borrowed_base:.6f} {quote} {borrowed_quote:.2f} | est_eq: {est_eq:.2f} {quote}"
                )
        except Exception as e:
            self._log(f"⚠️ Portfolio log error: {e}")

    def _execute_trade(self, symbol: str, signal: str):
        s = self._get_settings()
        api_key = s.get("api_key", "")
        api_secret = s.get("api_secret", "")
        if not api_key or not api_secret:
            self._log("❌ Trade: API/Secret mancanti.")
            return

        is_isolated = (s.get("margin_mode", "isolated") == "isolated")
        auto_borrow = bool(s.get("auto_borrow", True))
        auto_repay = bool(s.get("auto_repay", True))
        sl_pct = float(s.get("sl_pct", 0) or 0)
        tp_pct = float(s.get("tp_pct", 0) or 0)

        base, quote = _parse_symbol_parts(symbol)
        cli = BinanceMarginClient(api_key, api_secret, self._log)
        cli.sync_time()

        self._ensure_cached(cli, symbol, is_isolated)

        max_base = 0.0
        max_quote = 0.0
        if self._cached.get("symbol") == symbol:
            max_base = float(self._cached.get("max_base", 0) or 0)
            max_quote = float(self._cached.get("max_quote", 0) or 0)

        try:
            price = cli.ticker_price(symbol)
        except Exception as e:
            self._log(f"❌ Trade: ticker fail {e}")
            return

        try:
            pos, borrowed_base, borrowed_quote, free_base, free_quote = self._position_by_debt(cli, symbol, is_isolated, base, quote)
            self._log_portfolio_snapshot(cli, symbol, is_isolated, base, quote, price, tag="Before")
        except Exception as e:
            self._log(f"❌ Trade: read position fail {e}")
            return

        if signal == "buy" and pos == "long":
            self._log("✅ BUY ignorato: già LONG (debito quote presente).")
            return
        if signal == "sell" and pos == "short":
            self._log("✅ SELL ignorato: già SHORT (debito base presente).")
            return

        try:
            cli.cancel_open_orders(symbol, is_isolated)
        except:
            pass

        # Close opposite first
        try:
            if pos == "long" and signal == "sell":
                qty = free_base * 0.999
                if qty > 0:
                    self._log("🔁 Closing LONG...")
                    res_close = cli.close_long_sell(symbol, qty, is_isolated, auto_repay)
                    self._log(f"✅ CLOSE LONG OK — orderId={res_close.get('orderId', '')}")
                    time.sleep(0.6)
            elif pos == "short" and signal == "buy":
                qty = borrowed_base * 1.001
                if qty > 0:
                    self._log("🔁 Closing SHORT...")
                    res_close = cli.close_short_buy(symbol, qty, is_isolated, auto_repay)
                    self._log(f"✅ CLOSE SHORT OK — orderId={res_close.get('orderId', '')}")
                    time.sleep(0.6)
        except Exception as e:
            self._log(f"❌ Close FAIL: {e}")
            return

        # Re-check must be flat
        try:
            pos2, borrowed_base2, borrowed_quote2, free_base2, free_quote2 = self._position_by_debt(cli, symbol, is_isolated, base, quote)
            self._log_portfolio_snapshot(cli, symbol, is_isolated, base, quote, price, tag="AfterClose")
            if pos2 != "flat":
                self._log(f"⚠️ Non flat dopo close (pos={pos2}) — stop.")
                return
        except Exception as e:
            self._log(f"❌ Recheck fail: {e}")
            return

        # Open new position using ALL (free + maxBorrowable_safety)
        # Small open_safety to avoid insuff funds due fees/rounding.
        open_safety = 0.995

        try:
            # Exchange filters for sizing (same idea as X_Trader_Trading.py: amount_to_precision + min_qty)
            step = 0.0
            min_qty = 0.0
            try:
                filters = cli.exchange_filters(symbol)
                lot = filters.get("LOT_SIZE") or filters.get("MARKET_LOT_SIZE") or {}
                step = float(lot.get("stepSize", "0") or 0)
                min_qty = float(lot.get("minQty", "0") or 0)
            except Exception:
                step = 0.0
                min_qty = 0.0

            if signal == "buy":
                spend = free_quote2 + (max_quote if auto_borrow else 0.0)
                spend = max(0.0, spend * open_safety)
                qty = 0.0
                try:
                    if float(price) > 0:
                        qty = float(spend) / float(price)
                except Exception:
                    qty = 0.0

                if step > 0:
                    qty = _floor_to_step(qty, step)

                if qty <= 0 or (min_qty > 0 and qty < min_qty):
                    self._log("❌ BUY: qty troppo piccola per MARKET")
                    return

                self._log(f"🟢 OPEN BUY — qty={qty:.6f} {base} (ALL)")
                res = cli.market_buy_qty(symbol, qty, is_isolated, auto_borrow, auto_repay)
                side = "BUY"
            else:
                avail = free_base2 + (max_base if auto_borrow else 0.0)
                qty = max(0.0, float(avail) * open_safety)

                if step > 0:
                    qty = _floor_to_step(qty, step)

                if qty <= 0 or (min_qty > 0 and qty < min_qty):
                    self._log("❌ SELL: qty troppo piccola per MARKET")
                    return

                self._log(f"🔴 OPEN SELL — qty={qty:.6f} {base} (ALL)")
                res = cli.market_sell_qty(symbol, qty, is_isolated, auto_borrow, auto_repay)
                side = "SELL"
        except Exception as e:
            self._log(f"❌ OPEN FAIL: {e}")
            return

        order_id = res.get("orderId", "")
        try:
            with self._state_lock:
                self._ui_state["last_action"] = f"OPEN {side}"
                self._ui_state["symbol"] = symbol
        except Exception:
            pass

        self._log(f"✅ OPEN OK — side={side} orderId={order_id}")

        self._log_portfolio_snapshot(cli, symbol, is_isolated, base, quote, price, tag="AfterOpen")

        # Optional SL/TP
        try:
            exec_qty = float(res.get("executedQty", 0) or 0)
        except:
            exec_qty = 0.0

        try:
            _entry_price = float(price or 0.0)
            try:
                cq = float(res.get("cummulativeQuoteQty", 0) or 0.0)
                if float(exec_qty or 0.0) > 0.0 and cq > 0.0:
                    _entry_price = cq / float(exec_qty)
            except Exception:
                pass
            self._last_entry = {"symbol": symbol, "side": signal, "entry": float(_entry_price or 0.0), "qty": float(exec_qty or 0.0)}
        except Exception:
            pass

        try:
            if exec_qty > 0 and (sl_pct > 0 or tp_pct > 0):
                filters = cli.exchange_filters(symbol)
                lot = filters.get("LOT_SIZE") or filters.get("MARKET_LOT_SIZE") or {}
                step = float(lot.get("stepSize", "0") or 0)

                pf = filters.get("PRICE_FILTER") or {}
                tick = float(pf.get("tickSize", "0") or 0)

                if step > 0:
                    exec_qty = _floor_to_step(exec_qty, step)

                entry = float(price or 0.0)
                close_side = "SELL" if signal == "buy" else "BUY"

                # Calcolo SL/TP come nello script: STOP_LOSS_LIMIT + TAKE_PROFIT_LIMIT (con tentativo OCO)
                sl_price = 0.0
                tp_price = 0.0
                if signal == "buy":
                    if sl_pct > 0:
                        sl_price = entry * (1 - sl_pct)
                        sl_price = _quantize_to_step(sl_price, tick, ROUND_DOWN)
                    if tp_pct > 0:
                        tp_price = entry * (1 + tp_pct)
                        tp_price = _quantize_to_step(tp_price, tick, ROUND_DOWN)
                else:
                    if sl_pct > 0:
                        sl_price = entry * (1 + sl_pct)
                        sl_price = _quantize_to_step(sl_price, tick, ROUND_UP)
                    if tp_pct > 0:
                        tp_price = entry * (1 - tp_pct)
                        tp_price = _quantize_to_step(tp_price, tick, ROUND_UP)

                # Tentativo OCO quando entrambi presenti
                if sl_price > 0 and tp_price > 0:
                    try:
                        cli.place_oco_sl_tp(symbol, close_side, exec_qty, tp_price, sl_price, sl_price, is_isolated, auto_repay)
                        self._log(f"🛡️🎯 OCO SL/TP: SL={sl_price:.8f} TP={tp_price:.8f}")
                    except Exception:
                        # Fallback doppio: SL + TP separati (AUTO_REPAY)
                        try:
                            self._log(f"🛡️ SL: {sl_price:.8f}")
                            cli.place_sl_stop_limit(symbol, close_side, exec_qty, sl_price, is_isolated, auto_repay)
                        except Exception:
                            pass
                        try:
                            self._log(f"🎯 TP: {tp_price:.8f}")
                            cli.place_tp_take_profit_limit(symbol, close_side, exec_qty, tp_price, is_isolated, auto_repay)
                        except Exception:
                            pass
                else:
                    if sl_price > 0:
                        self._log(f"🛡️ SL: {sl_price:.8f}")
                        cli.place_sl_stop_limit(symbol, close_side, exec_qty, sl_price, is_isolated, auto_repay)
                    if tp_price > 0:
                        self._log(f"🎯 TP: {tp_price:.8f}")
                        cli.place_tp_take_profit_limit(symbol, close_side, exec_qty, tp_price, is_isolated, auto_repay)
        except Exception as e:
            self._log(f"⚠️ SL/TP non piazzati: {e}")

        # Email notify
        if bool(s.get("email_enabled", False)):
            try:
                sender = EmailSender.from_settings(s)
                subj = f"X-Trader AutoBot — {signal.upper()} {symbol}"
                body = (
                    f"Azione eseguita: {signal.upper()} {symbol}\n"
                    f"orderId: {order_id}\n"
                    f"UTC: {datetime.now(timezone.utc).isoformat()}\n"
                )
                sender.send(subj, body)
                self._log("📧 Email inviata.")
            except Exception as e:
                self._log(f"❌ Email FAIL: {e}")
