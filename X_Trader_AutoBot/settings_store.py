import json
import os
from pathlib import Path


class SettingsStore:
    def __init__(self, app_name: str):
        self.base = Path.home() / f".{app_name}"
        self.base.mkdir(parents=True, exist_ok=True)
        self.path = self.base / "settings.json"

    def load(self) -> dict:
        if not self.path.exists():
            return {}
        try:
            return json.loads(self.path.read_text(encoding="utf-8"))
        except:
            return {}

    def save(self, settings: dict):
        tmp = self.path.with_suffix(".tmp")
        tmp.write_text(json.dumps(settings, indent=2), encoding="utf-8")
        os.replace(tmp, self.path)
