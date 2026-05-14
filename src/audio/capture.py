"""
Audio capture — thin platform wrapper.

Auto-selects the correct backend based on the OS:
  - macOS  → Core Audio Process Tap helper (default on 14.2+), or BlackHole
             via sounddevice if `audio.device` names a real device.
  - Windows → WASAPI loopback (future)

Supports dual-source capture: a primary device (e.g. system audio for Discord)
and an optional microphone for the local player's voice. Both streams are
mixed together before being sent to the transcription pipeline.
"""

from __future__ import annotations

import logging
import platform
import threading
from collections.abc import Callable
from pathlib import Path

import numpy as np

from src.audio.backends.base import AudioCaptureBackend

logger = logging.getLogger(__name__)

# Sentinel names that route the primary device to the native macOS Process
# Tap backend instead of a real `sounddevice` input.
_SYSTEM_AUDIO_NAMES = frozenset({"system_audio", "__system_audio__", "System Audio"})

_BLACKHOLE_HAL_PLUGIN = Path("/Library/Audio/Plug-Ins/HAL/BlackHole2ch.driver")
_BLACKHOLE_DEVICE_NAME = "BlackHole 2ch"


def _blackhole_installed() -> bool:
    """True iff the BlackHole 2ch HAL plugin is present on disk."""
    return _BLACKHOLE_HAL_PLUGIN.is_dir()


def _make_backend(device_name: str, target_rate: int) -> AudioCaptureBackend:
    system = platform.system()
    if system == "Darwin":
        if device_name in _SYSTEM_AUDIO_NAMES:
            # Build-time availability check (OS version + helper present).
            # Runtime permission denial is handled later, in AudioCapture.start().
            from src.audio.macos_helper import check_os_supported
            if check_os_supported():
                from src.audio.backends.macos_system import MacOSSystemAudioBackend
                return MacOSSystemAudioBackend(
                    device_name=device_name, target_rate=target_rate
                )
            if _blackhole_installed():
                logger.warning(
                    "[audio] System audio capture unavailable; "
                    "falling back to BlackHole 2ch."
                )
                from src.audio.backends.macos import MacOSAudioBackend
                return MacOSAudioBackend(
                    device_name=_BLACKHOLE_DEVICE_NAME, target_rate=target_rate
                )
            raise RuntimeError(
                "System audio capture is unavailable (macOS 14.2+ required) "
                "and BlackHole 2ch is not installed. "
                "Install BlackHole or update macOS."
            )
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

    def __init__(self, device_name: str = "system_audio", target_rate: int = 16000) -> None:
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

        def _start_primary() -> None:
            if self._mic_backend:
                self._backend.start(_on_primary)
                self._mic_backend.start(self._on_mic_samples)
                print(f"[audio] Dual capture: {self._backend._device_name} + {self._mic_device_name}")
            else:
                self._backend.start(self._callback)

        # Runtime permission-denial fallback. If the primary is the native
        # macOS backend and it refuses to start (TCC denied, helper missing
        # at runtime, etc.), swap silently to BlackHole when installed.
        try:
            _start_primary()
        except Exception as exc:
            from src.audio.backends.macos_system import SystemAudioUnavailable
            if not isinstance(exc, SystemAudioUnavailable):
                raise
            if not _blackhole_installed():
                raise
            logger.warning(
                "[audio] System audio unavailable (%s); falling back to BlackHole 2ch.",
                exc,
            )
            try:
                self._backend.stop()
            except Exception:
                pass
            self._backend = _make_backend(_BLACKHOLE_DEVICE_NAME, self._target_rate)
            _start_primary()

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
