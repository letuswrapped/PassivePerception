"""macOS audio capture backend — BlackHole loopback via sounddevice."""

from __future__ import annotations

import threading
from collections.abc import Callable
from math import gcd

import numpy as np
import sounddevice as sd
from scipy.signal import resample_poly

from src.audio.backends.base import AudioCaptureBackend

TARGET_RATE = 16000


class MacOSAudioBackend(AudioCaptureBackend):
    """
    Captures system audio via BlackHole virtual audio driver.

    Captures at the device's native sample rate (usually 44.1 or 48 kHz)
    and resamples to 16 kHz for Whisper.
    """

    def __init__(self, device_name: str = "BlackHole 2ch", target_rate: int = TARGET_RATE) -> None:
        self._device_name = device_name
        self._target_rate = target_rate
        self._stream: sd.InputStream | None = None
        self._lock = threading.Lock()
        self._up = 1
        self._down = 1
        self._native_rate = target_rate

    def start(self, callback: Callable[[np.ndarray], None]) -> None:
        device = self.find_loopback_device() or self._find_by_name(self._device_name)
        if device is None:
            raise RuntimeError(
                "BlackHole not found. Install with: brew install blackhole-2ch\n"
                "Then set up a Multi-Output Device in Audio MIDI Setup."
            )

        self._native_rate = int(device["sample_rate"])
        g = gcd(self._target_rate, self._native_rate)
        self._up   = self._target_rate  // g
        self._down = self._native_rate  // g

        print(f"[audio/macos] {device['name']} @ {self._native_rate} Hz "
              f"→ resample to {self._target_rate} Hz")

        def _sd_callback(indata, frames, time, status):
            if status:
                print(f"[audio/macos] {status}")
            mono = indata[:, 0].copy()
            if self._up != self._down:
                mono = resample_poly(mono, self._up, self._down).astype(np.float32)
            callback(mono)

        self._stream = sd.InputStream(
            device=device["index"],
            channels=1,
            samplerate=self._native_rate,
            dtype="float32",
            blocksize=4096,
            callback=_sd_callback,
        )
        self._stream.start()

    def stop(self) -> None:
        if self._stream:
            self._stream.stop()
            self._stream.close()
            self._stream = None

    def list_devices(self) -> list[dict]:
        devices = []
        for idx, dev in enumerate(sd.query_devices()):
            if dev["max_input_channels"] > 0:
                devices.append({
                    "index": idx,
                    "name": dev["name"],
                    "channels": dev["max_input_channels"],
                    "sample_rate": int(dev["default_samplerate"]),
                })
        return devices

    def find_loopback_device(self) -> dict | None:
        for dev in self.list_devices():
            if "blackhole" in dev["name"].lower():
                return dev
        return None

    def _find_by_name(self, name: str) -> dict | None:
        name_lower = name.lower()
        for dev in self.list_devices():
            if name_lower in dev["name"].lower():
                return dev
        return None

    @property
    def sample_rate(self) -> int:
        return self._target_rate

    @property
    def is_running(self) -> bool:
        return self._stream is not None and self._stream.active
