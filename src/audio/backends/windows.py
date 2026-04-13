"""Windows audio capture backend — WASAPI loopback (future implementation)."""

from __future__ import annotations

from collections.abc import Callable

import numpy as np

from src.audio.backends.base import AudioCaptureBackend


class WindowsAudioBackend(AudioCaptureBackend):
    """
    Captures system audio via WASAPI loopback on Windows.

    Requires: pip install pyaudiowpatch
    No virtual audio driver needed — WASAPI provides system audio natively.

    TODO: Implement when adding Windows support.
    """

    def __init__(self, device_name: str = "", target_rate: int = 16000) -> None:
        self._device_name = device_name
        self._target_rate = target_rate
        self._running = False

    def start(self, callback: Callable[[np.ndarray], None]) -> None:
        raise NotImplementedError(
            "Windows audio capture is not yet implemented.\n"
            "Coming soon: WASAPI loopback via pyaudiowpatch."
        )

    def stop(self) -> None:
        self._running = False

    def list_devices(self) -> list[dict]:
        raise NotImplementedError("Windows audio backend not yet implemented.")

    def find_loopback_device(self) -> dict | None:
        raise NotImplementedError("Windows audio backend not yet implemented.")

    @property
    def sample_rate(self) -> int:
        return self._target_rate

    @property
    def is_running(self) -> bool:
        return self._running
