import time
import threading

try:
    import requests
except Exception:  # requests missing
    requests = None


class SignalPoller:
    """
    Polls il segnale "latest" esattamente come fa il sito:
      - GET /api/latest (primario)
      - fallback: /.netlify/functions/latest
    "SITO CONNESSO" = ultima risposta OK entro TTL secondi.
    """
    def __init__(self, base_url: str, on_log, on_signal, poll_interval=1.2, ttl_connected=5.0):
        self.base_url = (base_url or "").rstrip("/")
        self._log = on_log
        self._on_signal = on_signal
        self.poll_interval = float(poll_interval)
        self.ttl_connected = float(ttl_connected)

        self._thr = None
        self._stop = threading.Event()
        self._last_ok = 0.0
        self._last_err = ""
        self._last_at = None

        self._endpoints = [
            "/api/latest",
            "/.netlify/functions/latest",
        ]
        self._active_idx = 0

    def start(self):
        if self._thr and self._thr.is_alive():
            return
        self._thr = threading.Thread(target=self._run, daemon=True)
        self._thr.start()

    def stop(self):
        self._stop.set()

    def is_connected(self) -> bool:
        return (time.time() - self._last_ok) <= self.ttl_connected

    def last_error(self) -> str:
        return self._last_err or ""

    def _mk_url(self, endpoint: str) -> str:
        if endpoint.startswith("http://") or endpoint.startswith("https://"):
            return endpoint
        return self.base_url + endpoint

    def _next_endpoint(self):
        self._active_idx = (self._active_idx + 1) % max(1, len(self._endpoints))

    def _run(self):
        if requests is None:
            self._last_err = "requests_non_installato"
            self._log("❌ SignalPoller: libreria 'requests' mancante. Installa: pip install requests")
            return

        self._log(f"📡 SignalPoller ON — {self.base_url}")

        while not self._stop.is_set():
            try:
                ts_ms = int(time.time() * 1000)

                endpoint = self._endpoints[self._active_idx]
                u = self._mk_url(endpoint)

                if "?" in u:
                    u = u + f"&nocache={ts_ms}"
                else:
                    u = u + f"?nocache={ts_ms}"

                r = requests.get(
                    u,
                    headers={"Cache-Control": "no-store", "Pragma": "no-cache"},
                    timeout=8
                )
                if r.status_code != 200:
                    raise RuntimeError(f"http_{r.status_code}")

                j = r.json()
                self._last_ok = time.time()
                self._last_err = ""

                if isinstance(j, dict):
                    at = j.get("at") or j.get("timestamp_utc") or j.get("ts") or None
                    if at is not None and at == self._last_at:
                        # duplicato
                        time.sleep(self.poll_interval)
                        continue
                    self._last_at = at

                    sig = str(j.get("signal", "hold")).upper()
                    pair = str(j.get("pair") or j.get("symbol") or "").upper()
                    if not pair:
                        pair = None

                    self._on_signal(j)

            except Exception as e:
                self._last_err = str(e)

                # Se fallisce, prova fallback endpoint
                self._next_endpoint()

                # avoid spam: log every ~5s during continuous errors
                if int(time.time()) % 5 == 0:
                    endpoint = self._endpoints[self._active_idx] if self._endpoints else "?"
                    self._log(f"⚠️ SignalPoller ERR: {e} (switch -> {endpoint})")

            time.sleep(self.poll_interval)
