"""
Audio capture — thin platform wrapper.

Auto-selects the correct backend based on the OS:
  - macOS  → BlackHole via sounddevice
  - Windows → WASAPI loopback (future)
"""

from __future__ import annotations

import platform
import sys
from collections.abc import Callable

import numpy as np

from src.audio.backends.base import AudioCaptureBackend


def _make_backend(device_name: str, target_rate: int) -> AudioCaptureBackend:
    system = platform.system()
    if system == "Darwin":
        from src.audio.backends.macos import MacOSAudioBackend
        return MacOSAudioBackend(device_name=device_name, target_rate=target_rate)
    elif system == "Windows":
        from src.audio.backends.windows import WindowsAudioBackend
        return WindowsAudioBackend(device_name=device_name, target_rate=target_rate)
    else:
        raise RuntimeError(f"Unsupported platform: {system}")


class AudioCapture:
    """Platform-agnostic audio capture. Delegates to the appropriate backend."""

    def __init__(self, device_name: str = "BlackHole 2ch", target_rate: int = 16000) -> None:
        self._backend = _make_backend(device_name, target_rate)

    def add_callback(self, cb: Callable[[np.ndarray], None]) -> None:
        self._callback = cb

    def start(self) -> None:
        self._backend.start(self._callback)

    def stop(self) -> None:
        self._backend.stop()

    @property
    def sample_rate(self) -> int:
        return self._backend.sample_rate

    @property
    def is_running(self) -> bool:
        return self._backend.is_running
