"""
config_manager.py - Persistent JSON configuration handler for DocuStream.

Loads settings from ``config.json`` (created from ``config.default.json`` on
first run), exposes them as a plain dictionary, and persists any changes
atomically so a crash can never corrupt the user's settings.
"""

import json
import os
import shutil
import tempfile
import threading
from pathlib import Path

# Absolute path of the directory that contains this file. All paths are
# resolved relative to it so DocuStream works from any working directory,
# which is essential for a portable / embedded-Python deployment.
BASE_DIR = Path(__file__).resolve().parent

DEFAULT_CONFIG_PATH = BASE_DIR / "config.default.json"
USER_CONFIG_PATH = BASE_DIR / "config.json"


class ConfigManager:
    """Thread-safe JSON configuration store with atomic persistence."""

    def __init__(self, user_path: Path = USER_CONFIG_PATH,
                 default_path: Path = DEFAULT_CONFIG_PATH):
        self._lock = threading.RLock()
        self._user_path = Path(user_path)
        self._default_path = Path(default_path)
        self._config = self._load()

    def _load(self) -> dict:
        """Load user config, creating it from the template if needed."""
        with self._lock:
            if not self._user_path.exists():
                if self._default_path.exists():
                    shutil.copyfile(self._default_path, self._user_path)
                else:
                    # Last-resort minimal defaults; never crash at startup.
                    self._user_path.write_text(
                        json.dumps(_minimal_defaults(), indent=2),
                        encoding="utf-8",
                    )
            try:
                with open(self._user_path, "r", encoding="utf-8") as fh:
                    data = json.load(fh)
            except (json.JSONDecodeError, OSError):
                # Corrupted config: back it up and regenerate from template.
                backup = self._user_path.with_suffix(".corrupt.json")
                try:
                    shutil.copyfile(self._user_path, backup)
                except OSError:
                    pass
                if self._default_path.exists():
                    shutil.copyfile(self._default_path, self._user_path)
                with open(self._user_path, "r", encoding="utf-8") as fh:
                    data = json.load(fh)

            # Merge in any keys that are new in the default template so that
            # upgrades never lose newly introduced settings.
            if self._default_path.exists():
                with open(self._default_path, "r", encoding="utf-8") as fh:
                    defaults = json.load(fh)
                data = self._deep_merge(defaults, data)
            return data

    @staticmethod
    def _deep_merge(defaults: dict, user: dict) -> dict:
        """Return ``user`` values with missing keys filled from ``defaults``."""
        merged = dict(defaults)
        for key, value in user.items():
            if key in merged and isinstance(merged[key], dict) \
                    and isinstance(value, dict):
                merged[key] = ConfigManager._deep_merge(merged[key], value)
            else:
                merged[key] = value
        return merged

    def save(self) -> None:
        """Persist the configuration atomically (write-temp-then-rename)."""
        with self._lock:
            fd, tmp_name = tempfile.mkstemp(
                dir=str(self._user_path.parent), suffix=".tmp"
            )
            try:
                with os.fdopen(fd, "w", encoding="utf-8") as fh:
                    json.dump(self._config, fh, indent=2)
                os.replace(tmp_name, self._user_path)
            except OSError:
                try:
                    os.unlink(tmp_name)
                except OSError:
                    pass
                raise

    def get(self, key: str, default=None):
        """Get a top-level setting (supports dotted paths, e.g. 'anythingllm.base_url')."""
        with self._lock:
            node = self._config
            for part in key.split("."):
                if not isinstance(node, dict) or part not in node:
                    return default
                node = node[part]
            return node

    def set(self, key: str, value, persist: bool = True) -> None:
        """Set a setting (dotted paths supported) and optionally save to disk."""
        with self._lock:
            parts = key.split(".")
            node = self._config
            for part in parts[:-1]:
                node = node.setdefault(part, {})
                if not isinstance(node, dict):
                    raise KeyError(f"Cannot set '{key}': '{part}' is not a mapping")
            node[parts[-1]] = value
            if persist:
                self.save()

    def as_dict(self) -> dict:
        """Return a shallow copy of the full configuration."""
        with self._lock:
            return json.loads(json.dumps(self._config))

    @property
    def recordings_dir(self) -> Path:
        """Absolute path of the folder where recordings and transcripts live."""
        with self._lock:
            raw = self._config.get("recordings_dir", "recordings")
            path = Path(raw)
            if not path.is_absolute():
                path = BASE_DIR / path
            path.mkdir(parents=True, exist_ok=True)
            return path


def _minimal_defaults() -> dict:
    """Fallback defaults used if even the template file is missing."""
    return {
        "whisper_model": "small",
        "whisper_device": "cpu",
        "whisper_compute_type": "int8",
        "whisper_language": "auto",
        "beam_size": 5,
        "max_call_duration_min": 120,
        "silence_timeout_min": 8,
        "silence_rms_threshold": 120.0,
        "retention_mode": "keep_days",
        "retention_days": 3,
        "last_project": "General",
        "last_tags": "",
        "last_participants": "",
        "anythingllm": {
            "base_url": "http://localhost:3001/api/v1",
            "api_key": "",
            "auto_upload": False,
            "default_workspace": "",
        },
        "recordings_dir": "recordings",
        "audio": {"loopback": "", "microphone": ""},
        "ui": {"appearance_mode": "dark", "color_theme": "blue"},
    }
