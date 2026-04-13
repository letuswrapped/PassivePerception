"""Abstract base class for audio capture backends."""

from __future__ import annotations

from abc import ABC, abstractmethod
from collections.abc import Callable

import numpy as np


class AudioCaptureBackend(ABC):
    """
    Platform-specific audio capture backend.

    Implementations:
      - macos.py  — BlackHole via sounddevice (macOS)
      - windows.py — WASAPI loopback (Windows, future)

    The callback receives mono float32 samples at the target sample rate.
    It is called from the audio thread and must be non-blocking.
    """

    @abstractmethod
    def start(self, callback: Callable[[np.ndarray], None]) -> None:
        """Open the audio stream and begin calling callback with PCM frames."""
        ...

    @abstractmethod
    def stop(self) -> None:
        """Stop the audio stream and release the device."""
        ...

    @abstractmethod
    def list_devices(self) -> list[dict]:
        """Return all available loopback/input devices."""
        ...

    @abstractmethod
    def find_loopback_device(self) -> dict | None:
        """Return the preferred loopback device (BlackHole, WASAPI loopback, etc.)."""
        ...

    @property
    @abstractmethod
    def sample_rate(self) -> int:
        """Sample rate delivered to the callback (always TARGET_RATE after resampling)."""
        ...

    @property
    @abstractmethod
    def is_running(self) -> bool:
        ...
