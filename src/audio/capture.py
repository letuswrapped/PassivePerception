"""
Audio capture — thin platform wrapper.

Auto-selects the correct backend based on the OS:
  - macOS  → BlackHole via sounddevice
  - Windows → WASAPI loopback (future)

Supports dual-source capture: a primary device (e.g. BlackHole for Discord)
and an optional microphone for the local player's voice. Both streams are
mixed together before being sent to the transcription pipeline.
"""

from __future__ import annotations

import platform
import threading
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
    """
    Platform-agnostic audio capture with optional dual-source mixing.

    Primary device: captures app/Discord audio (e.g. BlackHole 2ch)
    Mic device:     captures local player's microphone (optional)

    When both are active, audio streams are mixed together so the
    transcription pipeline hears everyone — remote players AND the local user.
    """

    def __init__(self, device_name: str = "BlackHole 2ch", target_rate: int = 16000) -> None:
        self._backend = _make_backend(device_name, target_rate)
        self._mic_backend: AudioCaptureBackend | None = None
        self._mic_device_name: str | None = None
        self._target_rate = target_rate
        self._callback: Callable[[np.ndarray], None] | None = None

        # Mixing state — accumulates mic samples between primary callbacks
        self._mic_buffer: list[np.ndarray] = []
        self._mic_lock = threading.Lock()

    def set_mic_device(self, device_name: str | None) -> None:
        """Set or clear the microphone device for dual-source capture."""
        # Stop existing mic if running
        if self._mic_backend is not None:
            self._mic_backend.stop()
            self._mic_backend = None

        self._mic_device_name = device_name
        if not device_name:
            return

        self._mic_backend = _make_backend(device_name, self._target_rate)

        # If already capturing, start the mic stream immediately
        if self.is_running and self._callback:
            self._mic_backend.start(self._on_mic_samples)
            print(f"[audio] Mic capture started: {device_name}")

    def add_callback(self, cb: Callable[[np.ndarray], None]) -> None:
        self._callback = cb

    def start(self) -> None:
        def _on_primary(samples: np.ndarray) -> None:
            """Mix mic audio into the primary stream."""
            mixed = samples
            with self._mic_lock:
                if self._mic_buffer:
                    mic_audio = np.concatenate(self._mic_buffer)
                    self._mic_buffer.clear()
                    # Match lengths — truncate or pad to align
                    min_len = min(len(mixed), len(mic_audio))
                    mixed = mixed[:min_len] + mic_audio[:min_len]
                    # If primary is longer, append the remainder
                    if len(samples) > min_len:
                        mixed = np.concatenate([mixed, samples[min_len:]])
                    # Clip to prevent distortion
                    mixed = np.clip(mixed, -1.0, 1.0)
            self._callback(mixed)

        if self._mic_backend:
            # Dual-source: mix mic into primary
            self._backend.start(_on_primary)
            self._mic_backend.start(self._on_mic_samples)
            print(f"[audio] Dual capture: {self._backend._device_name} + {self._mic_device_name}")
        else:
            # Single source — direct passthrough
            self._backend.start(self._callback)

    def _on_mic_samples(self, samples: np.ndarray) -> None:
        """Accumulate mic samples to be mixed on the next primary callback."""
        with self._mic_lock:
            self._mic_buffer.append(samples)

    def stop(self) -> None:
        self._backend.stop()
        if self._mic_backend:
            self._mic_backend.stop()
        with self._mic_lock:
            self._mic_buffer.clear()

    @property
    def sample_rate(self) -> int:
        return self._backend.sample_rate

    @property
    def is_running(self) -> bool:
        return self._backend.is_running
