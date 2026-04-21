"""
Platform detection + per-OS path helpers.

Centralizes the one-off macOS/Windows branches so call sites don't sprinkle
`sys.platform` checks everywhere. On macOS this module returns the exact
same paths the app has always used; on Windows it returns the `%APPDATA%`
equivalent. Nothing in here is destructive on macOS.
"""

from __future__ import annotations

import os
import sys
from pathlib import Path


APP_SUPPORT_FOLDER_NAME = "Passive Perception"


def is_macos() -> bool:
    return sys.platform == "darwin"


def is_windows() -> bool:
    return sys.platform == "win32"


def app_support_dir() -> Path:
    """
    Return the per-user writable directory where the app stores secrets
    (`.env`), logs, and local session artifacts.

    Resolution order:
      1. `PP_SUPPORT_DIR` environment variable (explicit override for dev / tests)
      2. Platform default:
         - macOS:   ~/Library/Application Support/Passive Perception
         - Windows: %APPDATA%\\Passive Perception   (typically ~/AppData/Roaming/Passive Perception)
         - Other:   ~/.passive-perception           (fallback for dev on Linux)
    """
    override = os.environ.get("PP_SUPPORT_DIR")
    if override:
        return Path(override)

    if is_macos():
        return Path.home() / "Library" / "Application Support" / APP_SUPPORT_FOLDER_NAME
    if is_windows():
        appdata = os.environ.get("APPDATA")
        if appdata:
            return Path(appdata) / APP_SUPPORT_FOLDER_NAME
        return Path.home() / "AppData" / "Roaming" / APP_SUPPORT_FOLDER_NAME
    return Path.home() / ".passive-perception"


def ensure_app_support_dir() -> Path:
    """Convenience: resolve and mkdir -p the support dir, return it."""
    path = app_support_dir()
    path.mkdir(parents=True, exist_ok=True)
    return path


def obsidian_vault_candidates() -> list[Path]:
    """
    Reasonable starting points to look for an Obsidian vault during auto-detect
    or as default picker locations. Ordered best-first.

    On Windows we include the OneDrive-redirected Documents because many
    users end up with Documents living under OneDrive without realizing it.
    """
    home = Path.home()
    candidates: list[Path] = []
    if is_windows():
        candidates.append(home / "OneDrive" / "Documents" / "Obsidian")
    candidates.append(home / "Documents" / "Obsidian")
    return [p for p in candidates if p.exists()]


def default_obsidian_campaigns_dir() -> Path:
    """
    Where to store persistent campaign JSON files if an Obsidian vault is
    available. First hit from `obsidian_vault_candidates()` wins; falls back
    to the app support dir if none found.
    """
    for vault_root in obsidian_vault_candidates():
        pp_folder = vault_root / "PassivePerception" / "campaigns"
        if pp_folder.parent.exists() or vault_root.exists():
            return pp_folder
    return app_support_dir() / "campaigns"


def open_audio_settings() -> bool:
    """
    Open the OS's audio settings UI. Used for the "Open Audio Settings" button
    in onboarding + the Audio settings pane. Returns True if the command was
    dispatched, False if no handler is available on this platform.
    """
    import subprocess
    try:
        if is_macos():
            subprocess.Popen(["open", "/Applications/Utilities/Audio MIDI Setup.app"])
            return True
        if is_windows():
            # ms-settings:sound is the modern Windows 10/11 sound settings page.
            # os.startfile handles the Shell protocol launch correctly.
            os.startfile("ms-settings:sound")   # type: ignore[attr-defined]
            return True
    except Exception:
        return False
    return False


def pick_folder(prompt: str = "Select a folder") -> str | None:
    """
    Cross-platform folder picker. Returns the selected path or None if cancelled.

    macOS keeps the existing osascript dialog so behavior is bit-identical with
    prior releases. Windows uses tkinter.filedialog which ships with CPython —
    no extra install burden.
    """
    import subprocess
    if is_macos():
        try:
            result = subprocess.run(
                [
                    "osascript", "-e",
                    f'set theFolder to choose folder with prompt "{prompt}"',
                    "-e", 'POSIX path of theFolder',
                ],
                capture_output=True, text=True, timeout=60,
            )
            if result.returncode != 0 or not result.stdout.strip():
                return None
            return result.stdout.strip().rstrip("/")
        except Exception:
            return None

    # Windows / fallback — tkinter dialog. Created + destroyed each call so we
    # don't hold onto a hidden root window.
    try:
        import tkinter as tk
        from tkinter import filedialog
        root = tk.Tk()
        root.withdraw()
        root.attributes("-topmost", True)
        try:
            path = filedialog.askdirectory(title=prompt, mustexist=True)
        finally:
            root.destroy()
        return path or None
    except Exception:
        return None
