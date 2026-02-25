import os
import json
import time
import uuid
import hashlib
import platform
import threading

import requests


def _sha256(s: str) -> str:
    try:
        return hashlib.sha256(s.encode("utf-8", errors="ignore")).hexdigest()
    except Exception:
        return ""


def build_fingerprint() -> str:
    """
    Stable client fingerprint (not secret).
    - hostname + platform + mac (uuid.getnode) + user
    - sha256 to avoid exposing raw info
    """
    try:
        host = platform.node() or ""
        plat = platform.platform() or ""
        mac = str(uuid.getnode())
        user = ""
        try:
            user = os.getlogin() or ""
        except Exception:
            user = ""
        base = "|".join([host, plat, mac, user])
        return _sha256(base)
    except Exception:
        return _sha256(str(time.time()))


class LicenseClient:
    def __init__(self, base_url: str, on_log=None, timeout: float = 8.0):
        self.base_url = (base_url or "").rstrip("/")
        self.on_log = on_log
        self.timeout = float(timeout)

        self.license_key = ""
        self.fingerprint = build_fingerprint()
        self.session_id = ""

        # Extra info (optional)
        self.plan_code = ""
        self.expiry_ts = 0

        self._hb_thr = None
        self._hb_stop = threading.Event()
        self._last_status = {"ok": False, "reason": "not_checked", "plan_code": "", "expiry_ts": 0}

    def _log(self, msg: str):
        try:
            if self.on_log:
                self.on_log(msg)
        except Exception:
            pass

    def configure(self, license_key: str):
        self.license_key = (license_key or "").strip()

    def activate(self, version: str = "") -> bool:
        if not self.base_url:
            self._last_status = {"ok": False, "reason": "missing_server_url", "plan_code": "", "expiry_ts": 0}
            return False
        if not self.license_key:
            self._last_status = {"ok": False, "reason": "missing_license_key", "plan_code": "", "expiry_ts": 0}
            return False

        url = self.base_url + "/api/activate"
        payload = {
            "license_key": self.license_key,
            "fingerprint": self.fingerprint,
            "version": version or "",
        }
        try:
            r = requests.post(url, json=payload, timeout=self.timeout)
            data = {}
            try:
                data = r.json()
            except Exception:
                data = {}

            if r.status_code != 200:
                reason = data.get("detail") or data.get("reason") or ("http_" + str(r.status_code))
                self.plan_code = ""
                self.expiry_ts = 0
                self._last_status = {"ok": False, "reason": reason, "plan_code": "", "expiry_ts": 0}
                return False

            if not data.get("ok"):
                reason = data.get("reason") or "activate_failed"
                self.plan_code = ""
                self.expiry_ts = 0
                self._last_status = {"ok": False, "reason": reason, "plan_code": "", "expiry_ts": 0}
                return False

            self.session_id = data.get("session_id") or ""
            self.plan_code = data.get("plan_code") or ""
            try:
                self.expiry_ts = int(data.get("expiry_ts") or 0)
            except Exception:
                self.expiry_ts = 0

            self._last_status = {"ok": True, "reason": "ok", "plan_code": self.plan_code, "expiry_ts": int(self.expiry_ts or 0)}
            return True
        except Exception as e:
            self.plan_code = ""
            self.expiry_ts = 0
            self._last_status = {"ok": False, "reason": "exception:" + str(e), "plan_code": "", "expiry_ts": 0}
            return False

    def start_heartbeat(self, interval_sec: float = 30.0, on_blocked=None):
        """
        Keep session alive and stop trading if server reports blocked/expired/conflict.
        """
        try:
            self.stop_heartbeat()
        except Exception:
            pass

        self._hb_stop.clear()

        def _run():
            while not self._hb_stop.is_set():
                ok, reason = self.heartbeat()
                if not ok:
                    try:
                        if on_blocked:
                            on_blocked(reason)
                    except Exception:
                        pass
                    return
                try:
                    time.sleep(float(interval_sec))
                except Exception:
                    time.sleep(30.0)

        self._hb_thr = threading.Thread(target=_run, daemon=True)
        self._hb_thr.start()

    def stop_heartbeat(self):
        try:
            self._hb_stop.set()
        except Exception:
            pass

    def heartbeat(self):
        if not self.base_url or not self.license_key or not self.session_id:
            self._last_status = {"ok": False, "reason": "missing_params", "plan_code": self.plan_code, "expiry_ts": int(self.expiry_ts or 0)}
            return False, "missing_params"

        url = self.base_url + "/api/heartbeat"
        payload = {
            "license_key": self.license_key,
            "fingerprint": self.fingerprint,
            "session_id": self.session_id,
        }
        try:
            r = requests.post(url, json=payload, timeout=self.timeout)
            data = {}
            try:
                data = r.json()
            except Exception:
                data = {}

            if r.status_code != 200:
                reason = data.get("detail") or data.get("reason") or ("http_" + str(r.status_code))
                self._last_status = {"ok": False, "reason": reason, "plan_code": self.plan_code, "expiry_ts": int(self.expiry_ts or 0)}
                return False, reason

            if not data.get("ok"):
                reason = data.get("reason") or "heartbeat_failed"
                self._last_status = {"ok": False, "reason": reason, "plan_code": self.plan_code, "expiry_ts": int(self.expiry_ts or 0)}
                return False, reason

            self.plan_code = data.get("plan_code") or self.plan_code
            try:
                self.expiry_ts = int(data.get("expiry_ts") or self.expiry_ts or 0)
            except Exception:
                pass

            self._last_status = {"ok": True, "reason": "ok", "plan_code": self.plan_code, "expiry_ts": int(self.expiry_ts or 0)}
            return True, "ok"
        except Exception as e:
            reason = "exception:" + str(e)
            self._last_status = {"ok": False, "reason": reason, "plan_code": self.plan_code, "expiry_ts": int(self.expiry_ts or 0)}
            return False, reason

    def last_status(self) -> dict:
        return dict(self._last_status)
