"""
Windows audio capture backend — WASAPI loopback via `soundcard`.

Why soundcard instead of sounddevice:
    `sounddevice` (our macOS lib) does NOT support WASAPI loopback. Upstream
    issue #281 has been open since 2020 with no plan to fix. `soundcard`
    (bastibe/SoundCard) is pure-Python CFFI, BSD-3, actively maintained, and
    has first-class loopback support via `sc.all_microphones(include_loopback=True)`.

Flow:
    1. Enumerate loopback mics matching each render endpoint (speakers,
       headphones, Bluetooth, virtual outputs).
    2. Pick the loopback of the user-selected output, or default to the
       system default render device.
    3. Open a recorder in a background thread (soundcard's `record()` is
       blocking). On each buffer: stereo→mono downmix, resample native
       rate → 16 kHz, hand the mono float32 array to the callback.

Known quirks we actively work around:
    - soundcard issue #166 (silent prefix): `.record()` may not start
      delivering samples until audio is actually playing. We don't try to
      fake samples — the transcription pipeline handles silence fine.
    - Single-channel WASAPI recording is buggy in soundcard — we always
      request `channels=2` (or the endpoint's native count) and downmix
      ourselves.
    - Spatial audio / Atmos can deliver 5.1 / 7.1 / even 8-channel float32.
      Downmix by averaging all channels.
"""

from __future__ import annotations

import logging
import threading
from collections.abc import Callable
from math import gcd

import numpy as np

from src.audio.backends.base import AudioCaptureBackend

logger = logging.getLogger(__name__)

TARGET_RATE = 16000
# ~50 ms at 48 kHz. Balances latency (callbacks not too tiny) against memory
# pressure (chunk size small enough that stop() returns quickly).
_BLOCKSIZE_FRAMES = 2400


class WindowsAudioBackend(AudioCaptureBackend):
    """
    Captures system audio via WASAPI loopback on Windows 10/11.

    No driver install required — WASAPI loopback is a first-party Windows
    API since Vista. Captures the full system mix on the chosen output
    endpoint, including Discord + any background audio.
    """

    def __init__(
        self,
        device_name: str = "",
        target_rate: int = TARGET_RATE,
    ) -> None:
        self._requested_device_name = device_name
        self._target_rate = target_rate
        self._running = False
        self._thread: threading.Thread | None = None
        self._stop_event = threading.Event()
        self._selected_id: str | None = None   # stable endpoint id, logged
        self._native_rate: int = 48000
        self._native_channels: int = 2
        self._up = 1
        self._down = 1

    # ── Public API ────────────────────────────────────────────────────────

    def start(self, callback: Callable[[np.ndarray], None]) -> None:
        import soundcard as sc

        loopback = self._pick_loopback(sc)
        if loopback is None:
            raise RuntimeError(
                "No WASAPI loopback device available. "
                "Windows needs at least one active audio output (headphones, "
                "speakers, or virtual device) — check Sound settings."
            )

        self._selected_id = getattr(loopback, "id", loopback.name)
        # Probe the endpoint — soundcard exposes device metadata via the
        # speaker side, not the loopback mic. Fetch the matched speaker to
        # read its native rate + channel count.
        matching_speaker = self._speaker_for_loopback(sc, loopback)
        if matching_speaker is not None and getattr(matching_speaker, "channels", None):
            self._native_channels = int(matching_speaker.channels)
        else:
            self._native_channels = 2

        # Most modern endpoints run 48 kHz shared-mode. soundcard does not
        # expose a native-rate query in a consistent way across releases, so
        # we request 48 kHz and let the OS resample if the endpoint is 44.1.
        self._native_rate = 48000
        g = gcd(self._target_rate, self._native_rate)
        self._up = self._target_rate // g
        self._down = self._native_rate // g

        logger.info(
            "[audio/windows] loopback=%s native=%dHz/%dch → resample to %dHz mono",
            loopback.name, self._native_rate, self._native_channels, self._target_rate,
        )

        self._stop_event.clear()
        self._running = True
        self._thread = threading.Thread(
            target=self._capture_loop,
            args=(loopback, callback),
            name="pp-audio-windows",
            daemon=True,
        )
        self._thread.start()

    def stop(self) -> None:
        self._stop_event.set()
        self._running = False
        if self._thread is not None:
            self._thread.join(timeout=2.0)
            self._thread = None

    def list_devices(self) -> list[dict]:
        """
        Return user-facing *output* endpoints. The UI picker shows outputs
        (headphones / speakers / Discord's virtual device etc); internally
        we resolve each to its loopback mic.
        """
        import soundcard as sc
        out: list[dict] = []
        try:
            default = sc.default_speaker()
            default_id = getattr(default, "id", default.name)
        except Exception:
            default_id = None
        for idx, spk in enumerate(sc.all_speakers()):
            out.append({
                "index": idx,
                "id": getattr(spk, "id", spk.name),
                "name": spk.name,
                "channels": getattr(spk, "channels", 2),
                "sample_rate": 48000,
                "is_default": getattr(spk, "id", spk.name) == default_id,
            })
        return out

    def find_loopback_device(self) -> dict | None:
        """Return the default output endpoint, resolved to loopback internally."""
        import soundcard as sc
        try:
            spk = sc.default_speaker()
        except Exception:
            return None
        return {
            "index": 0,
            "id": getattr(spk, "id", spk.name),
            "name": spk.name,
            "channels": getattr(spk, "channels", 2),
            "sample_rate": 48000,
            "is_default": True,
        }

    @property
    def sample_rate(self) -> int:
        return self._target_rate

    @property
    def is_running(self) -> bool:
        return self._running

    # ── Internals ─────────────────────────────────────────────────────────

    def _pick_loopback(self, sc):
        """Find the right loopback mic based on user selection or default."""
        name_lower = (self._requested_device_name or "").strip().lower()
        loopbacks = [m for m in sc.all_microphones(include_loopback=True) if getattr(m, "isloopback", False)]

        if name_lower:
            for m in loopbacks:
                if name_lower in m.name.lower():
                    return m
            logger.warning(
                "Requested loopback device %r not found; falling back to default speaker",
                self._requested_device_name,
            )

        # Default to the default speaker's matching loopback
        try:
            default_spk = sc.default_speaker()
        except Exception:
            return loopbacks[0] if loopbacks else None
        for m in loopbacks:
            if m.name == default_spk.name:
                return m
        # As a last resort return the first loopback endpoint
        return loopbacks[0] if loopbacks else None

    def _speaker_for_loopback(self, sc, loopback):
        """Best-effort: find the speaker object whose name matches this loopback."""
        for spk in sc.all_speakers():
            if spk.name == loopback.name:
                return spk
        return None

    def _capture_loop(self, loopback, callback: Callable[[np.ndarray], None]) -> None:
        """Runs in a background thread; owns the recorder context."""
        from scipy.signal import resample_poly
        try:
            with loopback.recorder(
                samplerate=self._native_rate,
                channels=self._native_channels,
                blocksize=_BLOCKSIZE_FRAMES,
            ) as rec:
                while not self._stop_event.is_set():
                    # record() returns shape (numframes, channels) float32.
                    # numframes can be less than requested if the stream is
                    # closing — defensive copy + shape check before handing off.
                    data = rec.record(numframes=_BLOCKSIZE_FRAMES)
                    if data is None or data.size == 0:
                        continue
                    if data.ndim == 1:
                        mono = data.astype(np.float32, copy=False)
                    else:
                        # Downmix: average all channels (handles stereo, 5.1,
                        # 7.1, and Atmos 8ch without special-casing).
                        mono = data.mean(axis=1).astype(np.float32, copy=False)
                    if self._up != self._down:
                        mono = resample_poly(mono, self._up, self._down).astype(np.float32)
                    callback(mono)
        except Exception as exc:
            logger.error("[audio/windows] capture loop failed: %s", exc)
        finally:
            self._running = False
