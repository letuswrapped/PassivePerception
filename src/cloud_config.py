"""
Cloud provider credentials — Deepgram + Gemini API keys.

Stored in a .env file inside the app's support directory
(~/Library/Application Support/Passive Perception/.env on macOS).

No secrets live in the repo or in config.yaml. The user enters keys via the
Settings panel; the backend persists them here and re-loads them on boot.
"""

from __future__ import annotations

import os
from pathlib import Path
from typing import Optional

from dotenv import load_dotenv, set_key

from src.platform_utils import ensure_app_support_dir


_APP_SUPPORT_DIR = ensure_app_support_dir()
_ENV_PATH = _APP_SUPPORT_DIR / ".env"


def load_keys() -> None:
    """Load API keys from the app's .env into os.environ."""
    if _ENV_PATH.exists():
        load_dotenv(_ENV_PATH, override=False)


def get_deepgram_key() -> Optional[str]:
    return os.environ.get("DEEPGRAM_API_KEY") or None


def get_gemini_key() -> Optional[str]:
    return os.environ.get("GEMINI_API_KEY") or None


def save_keys(deepgram: Optional[str] = None, gemini: Optional[str] = None) -> None:
    """Persist provided keys. Passing None leaves the existing value alone; passing '' clears it."""
    _ENV_PATH.touch(exist_ok=True)
    if deepgram is not None:
        if deepgram:
            set_key(str(_ENV_PATH), "DEEPGRAM_API_KEY", deepgram)
            os.environ["DEEPGRAM_API_KEY"] = deepgram
        else:
            _remove_key("DEEPGRAM_API_KEY")
    if gemini is not None:
        if gemini:
            set_key(str(_ENV_PATH), "GEMINI_API_KEY", gemini)
            os.environ["GEMINI_API_KEY"] = gemini
        else:
            _remove_key("GEMINI_API_KEY")


def status() -> dict:
    """Return {'deepgram': bool, 'gemini': bool} for the UI — never returns the actual key."""
    return {
        "deepgram": bool(get_deepgram_key()),
        "gemini": bool(get_gemini_key()),
    }


def _remove_key(name: str) -> None:
    if not _ENV_PATH.exists():
        return
    lines = [ln for ln in _ENV_PATH.read_text().splitlines() if not ln.startswith(f"{name}=")]
    _ENV_PATH.write_text("\n".join(lines) + ("\n" if lines else ""))
    os.environ.pop(name, None)
