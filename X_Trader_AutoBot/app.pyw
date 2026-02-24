import sys
import traceback
import ctypes

def _fatal(msg: str):
    try:
        ctypes.windll.user32.MessageBoxW(None, msg, "X-Trader AutoBot", 0x10)
    except Exception:
        pass
    try:
        with open("XTraderAutoBot_fatal.log", "a", encoding="utf-8") as f:
            f.write(msg + "\n")
    except Exception:
        pass

try:
    import queue
    import os
    import tkinter as tk
    from tkinter import messagebox


    # Optional chart (matplotlib)
    try:
        import matplotlib
        matplotlib.use('TkAgg')
        from matplotlib.figure import Figure
        from matplotlib.backends.backend_tkagg import FigureCanvasTkAgg
        HAVE_MPL = True
    except Exception:
        HAVE_MPL = False
        Figure = None
        FigureCanvasTkAgg = None

    import threading
    import time
    import requests
except Exception as e:
    _fatal("Tkinter non disponibile o errore import.\n\nDettaglio: " + str(e) + "\n\nInstalla Python con Tcl/Tk (Windows installer).")
    raise

from datetime import datetime
import webbrowser
from urllib.parse import quote


from settings_store import SettingsStore
from trader_engine import TraderEngine
from signal_poller import SignalPoller
from emailer import EmailSender

# =========================
#  FUTURISTIC UI THEME (match X_Trader_LogView.pyw)
# =========================
THEME_BG       = "#1A1F36"
THEME_PANEL    = "#0B1020"
THEME_CARD     = "#0F1730"
THEME_CARD_2   = "#101B3A"
THEME_BORDER   = "#1E2A4A"
THEME_TEXT     = "#E6F1FF"
THEME_MUTED    = "#9FB3C8"
THEME_ACCENT   = "#00FFE1"
THEME_WARNING  = "#C9B458"
THEME_SUCCESS  = "#3FD1A2"
THEME_DANGER   = "#C94A5A"

FONT_UI        = ("Segoe UI", 10)
FONT_UI_BOLD   = ("Segoe UI", 10, "bold")
FONT_SMALL     = ("Segoe UI", 9)
FONT_SMALL_B   = ("Segoe UI", 9, "bold")
FONT_TITLE     = ("Segoe UI Semibold", 18)
FONT_SUBTITLE  = ("Segoe UI", 12, "bold")
FONT_MONO      = ("Consolas", 9)

APP_NAME = "X-Trader AutoBot"

LOG_FILE = "XTraderAutoBot.log"

# Segnale del sito (come x-trader.cloud)
SITE_BASE_URL = "https://x-trader.cloud"


class App(tk.Tk):
    def __init__(self):
        super().__init__()
        self.title(APP_NAME)

        # Icon
        try:
            _icon_path = os.path.join(os.path.dirname(__file__), "assets", "xtrader_icon.png")
            if os.path.exists(_icon_path):
                self._icon_img = tk.PhotoImage(file=_icon_path)
                self.iconphoto(False, self._icon_img)
        except Exception:
            pass

        self.configure(bg=THEME_BG)
        self.geometry("1120x780")
        self.minsize(1040, 740)

        self._log_q = queue.Queue()
        self._sig_q = queue.Queue()

        self._store = SettingsStore(app_name="xtrader_autobot")
        self._settings = self._store.load()


        # Poller segnali (come il sito): GET /api/latest (fallback /.netlify/functions/latest)
        self._poller = SignalPoller(
            SITE_BASE_URL,
            on_log=self._log,
            on_signal=self._on_signal_from_site,
            poll_interval=1.2,
            ttl_connected=5.0,
        )
        self._site_clients = 0
        self._ws_ok = False
        self._bridge_ok = False

        self._engine = TraderEngine(
            settings_getter=self._get_runtime_settings,
            log_cb=self._log,
            signal_queue=self._sig_q,
            site_connected_getter=lambda: bool(self._poller.is_connected()) if hasattr(self, "_poller") else False,
        )

        self._build_ui()
        self._load_into_ui(self._settings)

        self._poller.start()
        self._engine.start()


        # Poll status -> badge SITO
        try:
            self._poll_connected = bool(self._poller.is_connected())
            if self._poll_connected:
                self._set_badge(self.badge_site, "SITO: CONNESSO", THEME_SUCCESS)
            else:
                self._set_badge(self.badge_site, "SITO: NON CONNESSO", THEME_WARNING)
        except Exception:
            pass

        # Decision/Position box
        try:
            st = self._engine.get_ui_state()
            dec = str(st.get("decision", "-") or "-").upper()
            pos = str(st.get("position", "unknown") or "unknown").upper()
            have_sl = bool(st.get("have_sl", False))
            have_tp = bool(st.get("have_tp", False))
            act = str(st.get("last_action", "-") or "-")
            eq = st.get("est_equity_quote", 0.0)

            if dec == "BUY":
                self.lbl_decision.configure(text="BUY", fg=THEME_SUCCESS)
            elif dec == "SELL":
                self.lbl_decision.configure(text="SELL", fg=THEME_DANGER)
            elif dec == "HOLD":
                self.lbl_decision.configure(text="HOLD", fg=THEME_WARNING)
            else:
                self.lbl_decision.configure(text=dec or "-", fg=THEME_MUTED)

            try:
                self.lbl_action.configure(text=act, fg=THEME_TEXT)
            except Exception:
                pass

            if pos == "LONG":
                self.lbl_position.configure(text="LONG", fg=THEME_SUCCESS)
            elif pos == "SHORT":
                self.lbl_position.configure(text="SHORT", fg=THEME_DANGER)
            elif pos == "FLAT":
                self.lbl_position.configure(text="FLAT", fg=THEME_WARNING)
            else:
                self.lbl_position.configure(text=pos, fg=THEME_MUTED)

            self.lbl_sltp.configure(text=f"SL:{'ON' if have_sl else 'OFF'}  TP:{'ON' if have_tp else 'OFF'}", fg=THEME_TEXT if (have_sl or have_tp) else THEME_MUTED)

            try:
                self.lbl_equity.configure(text=f"{float(eq):.2f}", fg=THEME_TEXT)
            except Exception:
                self.lbl_equity.configure(text="-", fg=THEME_MUTED)
        except Exception:
            pass

        # Chart update (1D)
        try:
            now_ts = time.time()
            sym = (self.symbol_var.get().strip().upper() or "BTCUSDC")
            if HAVE_MPL and not self._chart_fetching:
                if self._chart_symbol != sym:
                    self._chart_symbol = sym
                    self._chart_next_at = 0.0
                if now_ts >= float(self._chart_next_at or 0.0):
                    self._chart_fetching = True
                    self._chart_next_at = now_ts + 30.0

                    def _fetch_chart(_symbol):
                        try:
                            r = requests.get("https://api.binance.com/api/v3/klines", params={"symbol": _symbol, "interval": "5m", "limit": 288}, timeout=12)
                            r.raise_for_status()
                            k = r.json()
                            xs = []
                            ys = []
                            for row in k:
                                xs.append(int(row[0]))
                                ys.append(float(row[4]))
                            self._chart_data_q.put((_symbol, xs, ys))
                        except Exception as e:
                            self._chart_data_q.put((_symbol, None, None))
                        finally:
                            self._chart_fetching = False

                    threading.Thread(target=_fetch_chart, args=(sym,), daemon=True).start()

            # apply chart data if available
            try:
                _symbol, xs, ys = self._chart_data_q.get_nowait()
                if HAVE_MPL and xs and ys and self._chart_ax is not None:
                    self._chart_ax.clear()
                    xdt = [datetime.fromtimestamp(x/1000.0) for x in xs]
                    try:
                        self._chart_ax.set_facecolor(THEME_CARD_2)
                    except Exception:
                        pass
                    self._chart_ax.plot(xdt, ys, color=THEME_ACCENT, linewidth=1.2)
                    self._chart_ax.set_title(_symbol, color=THEME_TEXT)
                    try:
                        for _sp in self._chart_ax.spines.values():
                            _sp.set_color(THEME_BORDER)
                        self._chart_ax.tick_params(axis='x', colors=THEME_MUTED, labelsize=8)
                        self._chart_ax.tick_params(axis='y', colors=THEME_MUTED, labelsize=8)
                        self._chart_ax.grid(True, color=THEME_BORDER, alpha=0.6)
                    except Exception:
                        self._chart_ax.grid(True)
                    try:
                        self._chart_fig.autofmt_xdate()
                    except Exception:
                        pass
                    self._chart_canvas.draw()
            except queue.Empty:
                pass
        except Exception:
            pass

        self.after(120, self._ui_tick)

    # ---------------- UI ----------------
    def _build_ui(self):
        # Header
        header = tk.Frame(self, bg=THEME_BG)
        header.pack(fill="x", padx=14, pady=(12, 6))

        tk.Label(header, text=APP_NAME, bg=THEME_BG, fg=THEME_TEXT, font=FONT_TITLE).pack(side="left")

        self.badge_site = self._badge(header, "SITO: NON CONNESSO", THEME_WARNING)
        self.badge_site.pack(side="right", padx=(10, 0))
        self.badge_arm = self._badge(header, "STATO: STOP", THEME_DANGER)
        self.badge_arm.pack(side="right", padx=(10, 0))

        # Body (2 columns)
        body = tk.Frame(self, bg=THEME_BG)
        body.pack(fill="both", expand=True, padx=14, pady=(6, 12))
        body.columnconfigure(0, weight=1)
        body.columnconfigure(1, weight=1)
        body.rowconfigure(0, weight=0)
        body.rowconfigure(1, weight=1)

        left = tk.Frame(body, bg=THEME_BG)
        left.grid(row=0, column=0, sticky="nsew", padx=(0, 8))
        right = tk.Frame(body, bg=THEME_BG)
        right.grid(row=0, column=1, sticky="nsew", padx=(8, 0))

        # Cards
        self.card_trade = self._card(left, "TRADING (Binance Margin)")
        self.card_trade.pack(fill="x", pady=(0, 12))

        self.card_email = self._card(right, "EMAIL NOTIFY")
        self.card_email.pack(fill="x", pady=(0, 12))


        # Logs card spanning both columns
        self.card_logs = self._card(body, "ACTION LOG")
        self.card_logs.grid(row=1, column=0, columnspan=2, sticky="nsew")
        self.card_logs.rowconfigure(1, weight=1)
        self.card_logs.columnconfigure(0, weight=1)

        # ---- Trading fields ----
        self.api_key_var = tk.StringVar()
        self.api_secret_var = tk.StringVar()
        self.symbol_var = tk.StringVar()
        self.sl_pct_var = tk.StringVar()
        self.tp_pct_var = tk.StringVar()
        self.leverage_var = tk.StringVar()  # safety % (keep name to match X_Trader logic)
        self.margin_mode_var = tk.StringVar(value="isolated")
        self.auto_borrow_var = tk.BooleanVar(value=True)
        self.auto_repay_var = tk.BooleanVar(value=True)

        g = tk.Frame(self.card_trade, bg=THEME_CARD)
        g.pack(fill="x", padx=12, pady=(8, 10))
        self._labeled_entry(g, "API Key", self.api_key_var, row=0, show="•")
        self._labeled_entry(g, "API Secret", self.api_secret_var, row=1, show="•")
        self._labeled_entry(g, "Pair (es. BTCUSDC)", self.symbol_var, row=2)
        self._labeled_entry(g, "Stop Loss %", self.sl_pct_var, row=3)
        self._labeled_entry(g, "Take Profit %", self.tp_pct_var, row=4)
        self._labeled_entry(g, "Safety % (tolto da maxBorrowable)", self.leverage_var, row=5)

        row6 = tk.Frame(g, bg=THEME_CARD)
        row6.grid(row=6, column=0, columnspan=2, sticky="ew", pady=(12, 0))
        tk.Label(row6, text="Mode", bg=THEME_CARD, fg=THEME_MUTED, font=FONT_UI).pack(side="left")
        self._radio(row6, "Cross", self.margin_mode_var, "cross").pack(side="left", padx=10)
        self._radio(row6, "Isolated", self.margin_mode_var, "isolated").pack(side="left")

        row7 = tk.Frame(g, bg=THEME_CARD)
        row7.grid(row=7, column=0, columnspan=2, sticky="ew", pady=(10, 0))
        self._check(row7, "Auto Borrow", self.auto_borrow_var).pack(side="left")
        self._check(row7, "Auto Repay", self.auto_repay_var).pack(side="left", padx=18)

        # Buttons
        btns = tk.Frame(self.card_trade, bg=THEME_CARD)
        btns.pack(fill="x", padx=12, pady=(0, 12))
        self.btn_save = self._btn(btns, "SALVA", self._save_settings)
        self.btn_save.pack(side="left")
        self.btn_start = self._btn(btns, "START", self._start_trading, accent=True)
        self.btn_start.pack(side="left", padx=10)
        self.btn_stop = self._btn(btns, "STOP", self._stop_trading, danger=True)
        self.btn_stop.pack(side="left")

        # ---- Email fields ----
        self.email_enabled_var = tk.BooleanVar(value=False)
        self.email_provider_var = tk.StringVar(value="gmail")
        self.smtp_host_var = tk.StringVar()
        self.smtp_port_var = tk.StringVar()
        self.smtp_secure_var = tk.BooleanVar(value=True)
        self.smtp_user_var = tk.StringVar()
        self.smtp_pass_var = tk.StringVar()
        self.mail_to_var = tk.StringVar()

        eg = tk.Frame(self.card_email, bg=THEME_CARD)
        eg.pack(fill="x", padx=12, pady=(8, 10))
        self._check(eg, "Abilita email", self.email_enabled_var).grid(row=0, column=0, sticky="w", pady=(0, 6))
        self._labeled_option(eg, "Provider", self.email_provider_var, ["gmail", "outlook", "yahoo", "custom"], row=1)
        self._labeled_entry(eg, "SMTP Host", self.smtp_host_var, row=2)
        self._labeled_entry(eg, "SMTP Port", self.smtp_port_var, row=3)
        self._check(eg, "SMTP Secure (SSL)", self.smtp_secure_var).grid(row=4, column=0, columnspan=2, sticky="w", pady=(8, 6))
        self._labeled_entry(eg, "Email (login)", self.smtp_user_var, row=5)
        self._labeled_entry(eg, "Password / App Password", self.smtp_pass_var, row=6, show="•")
        self._labeled_entry(eg, "Invia a (To)", self.mail_to_var, row=7)

        ebtns = tk.Frame(self.card_email, bg=THEME_CARD)
        ebtns.pack(fill="x", padx=12, pady=(0, 12))
        self.btn_test_email = self._btn(ebtns, "Test Email", self._test_email)
        self.btn_test_email.pack(side="left")

        
        # ---- Logs + State + Chart ----
        lg_root = tk.Frame(self.card_logs, bg=THEME_CARD)
        lg_root.pack(fill="both", expand=True, padx=12, pady=(0, 12))
        lg_root.columnconfigure(0, weight=3)
        lg_root.columnconfigure(1, weight=2)
        lg_root.rowconfigure(0, weight=1)

        # Left: Action log
        lg_left = tk.Frame(lg_root, bg=THEME_CARD)
        lg_left.grid(row=0, column=0, sticky="nsew", padx=(0, 10))
        tk.Label(lg_left, text="(log su file: XTraderAutoBot.log)", bg=THEME_CARD, fg=THEME_MUTED, font=FONT_SMALL).pack(anchor="w", pady=(10, 4))
        self.txt_log = tk.Text(
            lg_left,
            bg=THEME_PANEL,
            fg=THEME_TEXT,
            insertbackground=THEME_ACCENT,
            font=FONT_MONO,
            relief="flat",
            highlightthickness=1,
            highlightbackground=THEME_BORDER,
            highlightcolor=THEME_ACCENT,
            wrap="word"
        )
        self.txt_log.pack(fill="both", expand=True)

        # Right: Decision/Position + 1D chart
        lg_right = tk.Frame(lg_root, bg=THEME_CARD)
        lg_right.grid(row=0, column=1, sticky="nsew")
        lg_right.rowconfigure(1, weight=1)
        lg_right.columnconfigure(0, weight=1)

        self.card_state = tk.Frame(lg_right, bg=THEME_CARD_2, highlightthickness=1, highlightbackground=THEME_BORDER)
        self.card_state.grid(row=0, column=0, sticky="ew", pady=(10, 10))
        self.card_state.columnconfigure(1, weight=1)

        tk.Label(self.card_state, text="DECISION / POSIZIONE", bg=THEME_CARD_2, fg=THEME_TEXT, font=FONT_SMALL_B).grid(row=0, column=0, columnspan=2, sticky="w", padx=10, pady=(10, 6))

        tk.Label(self.card_state, text="Decisione", bg=THEME_CARD_2, fg=THEME_MUTED, font=FONT_SMALL).grid(row=1, column=0, sticky="w", padx=10, pady=2)
        self.lbl_decision = tk.Label(self.card_state, text="-", bg=THEME_CARD_2, fg=THEME_WARNING, font=FONT_UI_BOLD)
        self.lbl_decision.grid(row=1, column=1, sticky="e", padx=10, pady=2)

        tk.Label(self.card_state, text="Azione", bg=THEME_CARD_2, fg=THEME_MUTED, font=FONT_SMALL).grid(row=2, column=0, sticky="w", padx=10, pady=2)
        self.lbl_action = tk.Label(self.card_state, text="-", bg=THEME_CARD_2, fg=THEME_TEXT, font=FONT_UI_BOLD)
        self.lbl_action.grid(row=2, column=1, sticky="e", padx=10, pady=2)

        tk.Label(self.card_state, text="Posizione (DEBT)", bg=THEME_CARD_2, fg=THEME_MUTED, font=FONT_SMALL).grid(row=3, column=0, sticky="w", padx=10, pady=2)
        self.lbl_position = tk.Label(self.card_state, text="UNKNOWN", bg=THEME_CARD_2, fg=THEME_MUTED, font=FONT_UI_BOLD)
        self.lbl_position.grid(row=3, column=1, sticky="e", padx=10, pady=2)

        tk.Label(self.card_state, text="SL / TP", bg=THEME_CARD_2, fg=THEME_MUTED, font=FONT_SMALL).grid(row=4, column=0, sticky="w", padx=10, pady=2)
        self.lbl_sltp = tk.Label(self.card_state, text="-", bg=THEME_CARD_2, fg=THEME_MUTED, font=FONT_UI_BOLD)
        self.lbl_sltp.grid(row=4, column=1, sticky="e", padx=10, pady=2)

        tk.Label(self.card_state, text="Equity stimata (Quote)", bg=THEME_CARD_2, fg=THEME_MUTED, font=FONT_SMALL).grid(row=5, column=0, sticky="w", padx=10, pady=(2, 10))
        self.lbl_equity = tk.Label(self.card_state, text="-", bg=THEME_CARD_2, fg=THEME_MUTED, font=FONT_UI_BOLD)
        self.lbl_equity.grid(row=5, column=1, sticky="e", padx=10, pady=(2, 10))

        self.card_chart = tk.Frame(lg_right, bg=THEME_CARD_2, highlightthickness=1, highlightbackground=THEME_BORDER)
        self.card_chart.grid(row=1, column=0, sticky="nsew")
        self.card_chart.rowconfigure(1, weight=1)
        self.card_chart.columnconfigure(0, weight=1)

        tk.Label(self.card_chart, text="GRAFICO 1 GIORNO", bg=THEME_CARD_2, fg=THEME_TEXT, font=FONT_SMALL_B).grid(row=0, column=0, sticky="w", padx=10, pady=(10, 6))

        self._chart_canvas = None
        self._chart_fig = None
        self._chart_ax = None
        self._chart_fetching = False
        self._chart_next_at = 0.0
        self._chart_symbol = None
        self._chart_data_q = queue.Queue()

        if HAVE_MPL:
            self._chart_fig = Figure(figsize=(4.2, 2.6), dpi=100, facecolor=THEME_CARD_2)
            self._chart_ax = self._chart_fig.add_subplot(111)
            try:
                self._chart_ax.set_facecolor(THEME_CARD_2)
                for _sp in self._chart_ax.spines.values():
                    _sp.set_color(THEME_BORDER)
                self._chart_ax.tick_params(axis='x', colors=THEME_MUTED, labelsize=8)
                self._chart_ax.tick_params(axis='y', colors=THEME_MUTED, labelsize=8)
                self._chart_ax.yaxis.label.set_color(THEME_MUTED)
                self._chart_ax.xaxis.label.set_color(THEME_MUTED)
            except Exception:
                pass
            self._chart_ax.set_title("")
            try:
                self._chart_ax.grid(True, color=THEME_BORDER, alpha=0.6)
            except Exception:
                self._chart_ax.grid(True)
            self._chart_canvas = FigureCanvasTkAgg(self._chart_fig, master=self.card_chart)
            self._chart_canvas.get_tk_widget().grid(row=1, column=0, sticky="nsew", padx=10, pady=(0, 10))
            try:
                self._chart_canvas.get_tk_widget().configure(bg=THEME_CARD_2, highlightthickness=0)
            except Exception:
                pass
        else:
            tk.Label(self.card_chart, text="matplotlib non installato (pip install matplotlib)", bg=THEME_CARD_2, fg=THEME_WARNING, font=FONT_SMALL).grid(row=1, column=0, sticky="w", padx=10, pady=(0, 10))

    def _card(self, parent, title):
        c = tk.Frame(parent, bg=THEME_CARD, highlightthickness=1, highlightbackground=THEME_BORDER)
        top = tk.Frame(c, bg=THEME_CARD)
        top.pack(fill="x", padx=12, pady=(10, 0))
        tk.Label(top, text=title, bg=THEME_CARD, fg=THEME_TEXT, font=FONT_SUBTITLE).pack(side="left")
        return c

    def _badge(self, parent, text, color):
        f = tk.Frame(parent, bg=THEME_PANEL, highlightthickness=1, highlightbackground=color)
        l = tk.Label(f, text=text, bg=THEME_PANEL, fg=color, font=FONT_UI_BOLD, padx=10, pady=6)
        l.pack()
        f._label = l
        return f

    def _set_badge(self, badge, text, color):
        badge.configure(highlightbackground=color)
        badge._label.configure(text=text, fg=color)

    def _btn(self, parent, text, cmd, accent=False, danger=False):
        bg = THEME_CARD_2
        fg = THEME_TEXT
        bd = THEME_BORDER
        if accent:
            bd = THEME_ACCENT
        if danger:
            bd = THEME_DANGER
        b = tk.Button(
            parent,
            text=text,
            command=cmd,
            bg=bg,
            fg=fg,
            activebackground=THEME_PANEL,
            activeforeground=fg,
            relief="flat",
            font=FONT_UI_BOLD,
            padx=14,
            pady=8,
            highlightthickness=1,
            highlightbackground=bd,
            highlightcolor=bd,
            cursor="hand2"
        )
        return b

    def _radio(self, parent, text, var, value):
        r = tk.Radiobutton(
            parent,
            text=text,
            variable=var,
            value=value,
            bg=THEME_CARD,
            fg=THEME_TEXT,
            selectcolor=THEME_PANEL,
            activebackground=THEME_CARD,
            activeforeground=THEME_TEXT,
            font=FONT_UI,
        )
        return r

    def _check(self, parent, text, var):
        c = tk.Checkbutton(
            parent,
            text=text,
            variable=var,
            bg=THEME_CARD,
            fg=THEME_TEXT,
            selectcolor=THEME_PANEL,
            activebackground=THEME_CARD,
            activeforeground=THEME_TEXT,
            font=FONT_UI,
        )
        return c

    def _labeled_entry(self, parent, label, var, row, show=None):
        tk.Label(parent, text=label, bg=THEME_CARD, fg=THEME_MUTED, font=FONT_UI).grid(row=row, column=0, sticky="w", pady=(6 if row else 0, 0), padx=(0, 10))
        e = tk.Entry(
            parent,
            textvariable=var,
            show=show,
            bg=THEME_PANEL,
            fg=THEME_TEXT,
            insertbackground=THEME_ACCENT,
            relief="flat",
            highlightthickness=1,
            highlightbackground=THEME_BORDER,
            highlightcolor=THEME_ACCENT
        )
        e.grid(row=row, column=1, sticky="ew", pady=(6 if row else 0, 0))
        parent.columnconfigure(1, weight=1)
        return e

    def _labeled_option(self, parent, label, var, values, row):
        tk.Label(parent, text=label, bg=THEME_CARD, fg=THEME_MUTED, font=FONT_UI).grid(row=row, column=0, sticky="w", pady=(6 if row else 0, 0), padx=(0, 10))
        opt = tk.OptionMenu(parent, var, *values)
        opt.configure(bg=THEME_PANEL, fg=THEME_TEXT, activebackground=THEME_CARD_2, activeforeground=THEME_TEXT, relief="flat", highlightthickness=1, highlightbackground=THEME_BORDER)
        opt["menu"].configure(bg=THEME_PANEL, fg=THEME_TEXT, activebackground=THEME_CARD_2, activeforeground=THEME_TEXT)
        opt.grid(row=row, column=1, sticky="ew", pady=(6 if row else 0, 0))
        parent.columnconfigure(1, weight=1)
        return opt

    # --------------- Settings ---------------
    def _load_into_ui(self, s):
        self.api_key_var.set(s.get("api_key", ""))
        self.api_secret_var.set(s.get("api_secret", ""))

        self.symbol_var.set(s.get("symbol", "BTCUSDC"))
        self.sl_pct_var.set(str(s.get("sl_pct", 0.8)))
        self.tp_pct_var.set(str(s.get("tp_pct", 0)))
        self.leverage_var.set(str(s.get("leverage", 9)))

        self.margin_mode_var.set(s.get("margin_mode", "isolated"))
        self.auto_borrow_var.set(bool(s.get("auto_borrow", True)))
        self.auto_repay_var.set(bool(s.get("auto_repay", True)))

        self.email_enabled_var.set(bool(s.get("email_enabled", False)))
        self.email_provider_var.set(s.get("email_provider", "gmail"))
        self.smtp_host_var.set(s.get("smtp_host", ""))
        self.smtp_port_var.set(str(s.get("smtp_port", "")))
        self.smtp_secure_var.set(bool(s.get("smtp_secure", True)))
        self.smtp_user_var.set(s.get("smtp_user", ""))
        self.smtp_pass_var.set(s.get("smtp_pass", ""))
        self.mail_to_var.set(s.get("mail_to", ""))

    def _collect_ui_settings(self):
        s = {}
        s["api_key"] = self.api_key_var.get().strip()
        s["api_secret"] = self.api_secret_var.get().strip()
        s["symbol"] = (self.symbol_var.get().strip().upper() or "BTCUSDC")

        try: s["sl_pct"] = float(self.sl_pct_var.get().strip() or "0")
        except: s["sl_pct"] = 0.0

        try: s["tp_pct"] = float(self.tp_pct_var.get().strip() or "0")
        except: s["tp_pct"] = 0.0

        try: s["leverage"] = float(self.leverage_var.get().strip() or "0")
        except: s["leverage"] = 0.0

        s["margin_mode"] = self.margin_mode_var.get() or "isolated"
        s["auto_borrow"] = bool(self.auto_borrow_var.get())
        s["auto_repay"] = bool(self.auto_repay_var.get())

        s["email_enabled"] = bool(self.email_enabled_var.get())
        s["email_provider"] = self.email_provider_var.get() or "custom"
        s["smtp_host"] = self.smtp_host_var.get().strip()
        try: s["smtp_port"] = int(self.smtp_port_var.get().strip() or "0")
        except: s["smtp_port"] = 0
        s["smtp_secure"] = bool(self.smtp_secure_var.get())
        s["smtp_user"] = self.smtp_user_var.get().strip()
        s["smtp_pass"] = self.smtp_pass_var.get().strip()
        s["mail_to"] = self.mail_to_var.get().strip()


        # fixed local
        return s

    def _save_settings(self):
        s = self._collect_ui_settings()
        self._store.save(s)
        self._settings = s
        self._log("✅ Settings salvati (local).")

    def _get_runtime_settings(self):
        return self._collect_ui_settings()

    # --------------- Actions ---------------
    def _start_trading(self):
        self._save_settings()
        self._engine.enable_trading(True)
        self._set_badge(self.badge_arm, "STATO: ARMED", THEME_SUCCESS)
        self._log("🟢 START: ARMED. Opera SOLO se SITO è CONNESSO.")

    def _stop_trading(self):
        self._engine.enable_trading(False)
        self._set_badge(self.badge_arm, "STATO: STOP", THEME_DANGER)
        self._log("🔴 STOP.")

    def _test_email(self):
        s = self._collect_ui_settings()
        if not s.get("smtp_user") or not s.get("smtp_pass") or not s.get("mail_to"):
            messagebox.showerror("Email", "Compila Email/Password/To prima del test.")
            return
        try:
            sender = EmailSender.from_settings(s)
            sender.send(
                subject="X-Trader AutoBot — Test",
                body="Test OK. Se leggi questa email, SMTP funziona."
            )
            self._log("✅ Test email inviato.")
        except Exception as e:
            self._log(f"❌ Test email FAIL: {e}")
            messagebox.showerror("Email", f"Errore invio: {e}")

    # --------------- Signal callbacks ---------------
    def _log(self, msg: str):
        self._log_q.put(msg)
        try:
            ts = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
            with open(LOG_FILE, "a", encoding="utf-8") as f:
                f.write(f"{ts} {msg}\n")
        except:
            pass

    def _ui_tick(self):
        while True:
            try:
                msg = self._log_q.get_nowait()
            except queue.Empty:
                break
            self.txt_log.insert("end", msg + "\n")
            self.txt_log.see("end")


        # Poll status -> badge SITO
        try:
            self._poll_connected = bool(self._poller.is_connected())
            if self._poll_connected:
                self._set_badge(self.badge_site, "SITO: CONNESSO", THEME_SUCCESS)
            else:
                self._set_badge(self.badge_site, "SITO: NON CONNESSO", THEME_WARNING)
        except Exception:
            pass


        # Decision/Position box (live)
        try:
            st = self._engine.get_ui_state()
            dec = str(st.get("decision", "-") or "-").upper()
            pos = str(st.get("position", "unknown") or "unknown").upper()
            have_sl = bool(st.get("have_sl", False))
            have_tp = bool(st.get("have_tp", False))
            act = str(st.get("last_action", "-") or "-")
            eq = st.get("est_equity_quote", 0.0)

            if dec == "BUY":
                self.lbl_decision.configure(text="BUY", fg=THEME_SUCCESS)
            elif dec == "SELL":
                self.lbl_decision.configure(text="SELL", fg=THEME_DANGER)
            elif dec == "HOLD":
                self.lbl_decision.configure(text="HOLD", fg=THEME_WARNING)
            else:
                self.lbl_decision.configure(text=dec or "-", fg=THEME_MUTED)

            try:
                self.lbl_action.configure(text=act, fg=THEME_TEXT)
            except Exception:
                pass

            if pos == "LONG":
                self.lbl_position.configure(text="LONG", fg=THEME_SUCCESS)
            elif pos == "SHORT":
                self.lbl_position.configure(text="SHORT", fg=THEME_DANGER)
            elif pos == "FLAT":
                self.lbl_position.configure(text="CHIUSA", fg=THEME_MUTED)
            else:
                self.lbl_position.configure(text=pos or "UNKNOWN", fg=THEME_MUTED)

            self.lbl_sltp.configure(
                text=f"SL:{'ON' if have_sl else 'OFF'}  TP:{'ON' if have_tp else 'OFF'}",
                fg=THEME_TEXT if (have_sl or have_tp) else THEME_MUTED
            )

            try:
                self.lbl_equity.configure(text=f"{float(eq):.2f}", fg=THEME_TEXT)
            except Exception:
                self.lbl_equity.configure(text="-", fg=THEME_MUTED)
        except Exception:
            pass

        # Chart update (1D) + apply data
        try:
            now_ts = time.time()
            sym = (self.symbol_var.get().strip().upper() or "BTCUSDC")

            if HAVE_MPL:
                if not getattr(self, "_chart_fetching", False):
                    if getattr(self, "_chart_symbol", None) != sym:
                        self._chart_symbol = sym
                        self._chart_next_at = 0.0

                    if now_ts >= float(getattr(self, "_chart_next_at", 0.0) or 0.0):
                        self._chart_fetching = True
                        self._chart_next_at = now_ts + 30.0

                        def _fetch_chart(_symbol):
                            try:
                                r = requests.get(
                                    "https://api.binance.com/api/v3/klines",
                                    params={"symbol": _symbol, "interval": "5m", "limit": 288},
                                    timeout=12
                                )
                                r.raise_for_status()
                                k = r.json()
                                xs = []
                                ys = []
                                for row in k:
                                    try:
                                        xs.append(int(row[0]))
                                        ys.append(float(row[4]))
                                    except Exception:
                                        pass
                                self._chart_data_q.put((_symbol, xs, ys))
                            except Exception:
                                self._chart_data_q.put((_symbol, None, None))
                            finally:
                                self._chart_fetching = False

                        threading.Thread(target=_fetch_chart, args=(sym,), daemon=True).start()

                # apply chart data (consume all)
                while True:
                    try:
                        _symbol, xs, ys = self._chart_data_q.get_nowait()
                    except queue.Empty:
                        break

                    if self._chart_ax is None:
                        continue

                    if xs and ys:
                        self._chart_ax.clear()
                        xdt = [datetime.fromtimestamp(x/1000.0) for x in xs]
                        self._chart_ax.plot(xdt, ys)
                        self._chart_ax.set_title(_symbol)
                        self._chart_ax.grid(True)
                        try:
                            self._chart_fig.autofmt_xdate()
                        except Exception:
                            pass
                        try:
                            self._chart_canvas.draw()
                        except Exception:
                            pass
                    else:
                        # show error/empty
                        try:
                            self._chart_ax.clear()
                            try:
                                self._chart_ax.set_facecolor(THEME_CARD_2)
                            except Exception:
                                pass
                            try:
                                self._chart_ax.set_title(sym, color=THEME_TEXT)
                            except Exception:
                                self._chart_ax.set_title(sym)
                            try:
                                self._chart_ax.text(0.5, 0.5, "NO DATA", ha="center", va="center", color=THEME_MUTED)
                            except Exception:
                                self._chart_ax.text(0.5, 0.5, "NO DATA", ha="center", va="center")
                            self._chart_ax.grid(False)
                            self._chart_canvas.draw()
                        except Exception:
                            pass
        except Exception:
            pass

        self.after(120, self._ui_tick)

    def _on_signal_from_site(self, payload: dict):
        try:
            if isinstance(payload, dict):
                self._sig_q.put(payload)
        except Exception:
            pass


    def on_close(self):
        try:
            self._engine.stop()
            self._poller.stop()
        except:
            pass
        self.destroy()


if __name__ == "__main__":
    try:
        app = App()
        app.protocol("WM_DELETE_WINDOW", app.on_close)
        app.mainloop()
    except Exception as e:
        tb = traceback.format_exc()
        try:
            with open("XTraderAutoBot_fatal.log", "a", encoding="utf-8") as f:
                f.write(tb + "\n")
        except:
            pass
        try:
            messagebox.showerror("X-Trader AutoBot", "Errore avvio GUI.\n\nGuarda XTraderAutoBot_fatal.log\n\n" + str(e))
        except:
            _fatal("Errore avvio GUI.\n\nGuarda XTraderAutoBot_fatal.log\n\n" + str(e))
        raise