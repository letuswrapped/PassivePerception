"""
Audio device enumeration — platform-aware.

Routes to the correct backend for listing and detecting capture devices.
"""

from __future__ import annotations

import platform


def _get_backend():
    system = platform.system()
    if system == "Darwin":
        from src.audio.backends.macos import MacOSAudioBackend
        return MacOSAudioBackend()
    elif system == "Windows":
        from src.audio.backends.windows import WindowsAudioBackend
        return WindowsAudioBackend()
    else:
        raise RuntimeError(f"Unsupported platform: {system}")


def list_input_devices() -> list[dict]:
    return _get_backend().list_devices()


def find_loopback_device() -> dict | None:
    return _get_backend().find_loopback_device()
