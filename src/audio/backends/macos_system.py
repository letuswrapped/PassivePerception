"""
Native macOS system audio capture via the pp-system-audio Swift helper.

The helper wraps Core Audio Process Taps (macOS 14.2+) and streams mono
float32 PCM to stdout. This backend spawns the helper, parses its header,
and pumps frames through the standard AudioCaptureBackend callback contract.

No BlackHole virtual audio device required. The Audio Capture permission
(NSAudioCaptureUsageDescription) is granted by the user on first session
start; the helper triggers TCC the first time it calls
AudioHardwareCreateProcessTap.
"""

from __future__ import annotations

import logging
import re
import subprocess
import threading
from collections.abc import Callable
from math import gcd

import numpy as np

from src.audio.backends.base import AudioCaptureBackend
from src.audio.macos_helper import check_os_supported, locate_system_audio_helper

logger = logging.getLogger(__name__)

TARGET_RATE = 16000


class SystemAudioUnavailable(RuntimeError):
    """
    Raised when the Process Tap helper can't deliver audio. Common causes:
    TCC permission denied, helper crash, or macOS version below 14.2.
    `AudioCapture.start()` catches this and falls back to BlackHole if the
    HAL plugin is installed.
    """


# Header format emitted by the Swift helper. See
# native/macos/pp-system-audio/README.md for the contract. The "PP1" prefix
# is the magic; bump to "PP2" if the format changes.
_HEADER_RE = re.compile(rb"^PP1 rate=(?P<rate>\d+) ch=(?P<ch>\d+) fmt=(?P<fmt>\w+)$")

# Bytes per loop iteration. 480 float32 samples = 10 ms at 48 kHz mono.
# Keeps queue depth shallow and matches the helper's IOProc cadence.
_FRAMES_PER_READ = 480
_BYTES_PER_SAMPLE = 4


class MacOSSystemAudioBackend(AudioCaptureBackend):
    """Reads mono float32 PCM from the Swift Process Tap helper subprocess."""

    # If the tap reports success at the API level (header emitted, IOProc
    # registered, AudioDeviceStart returns noErr) but produces zero audio
    # bytes within this window, treat the backend as broken and raise
    # SystemAudioUnavailable so AudioCapture falls through to BlackHole. This
    # is the failure mode we hit on macOS 15+/26 — the aggregate device claims
    # to be running but the IOProc never fires.
    _WATCHDOG_SECONDS = 3.0

    def __init__(self, device_name: str = "system_audio", target_rate: int = TARGET_RATE) -> None:
        self._device_name = "System Audio (Process Tap)"
        self._target_rate = target_rate
        self._running = False
        self._helper_proc: subprocess.Popen | None = None
        self._reader_thread: threading.Thread | None = None
        self._stderr_thread: threading.Thread | None = None
        self._stop_event = threading.Event()
        self._native_rate: int = 48000
        self._up = 1
        self._down = 1
        self._bytes_received = 0

    # ── Public API ────────────────────────────────────────────────────────

    def start(self, callback: Callable[[np.ndarray], None]) -> None:
        if not check_os_supported():
            raise SystemAudioUnavailable(
                "System audio capture requires macOS 14.2 or later, and the "
                "pp-system-audio helper must be present in the app bundle "
                "or built from source."
            )
        helper = locate_system_audio_helper()
        logger.info("[audio/macos-system] launching helper: %s", helper)

        proc = subprocess.Popen(
            [str(helper), "--mode", "system"],
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            bufsize=0,
            start_new_session=True,
        )
        self._helper_proc = proc

        # Read the header line. The helper writes it before any audio.
        assert proc.stdout is not None
        header_line = proc.stdout.readline()
        if not header_line:
            stderr_data = b""
            if proc.stderr is not None:
                try:
                    stderr_data = proc.stderr.read()
                except Exception:
                    pass
            try:
                proc.wait(timeout=1.0)
            except subprocess.TimeoutExpired:
                proc.kill()
            stderr_text = stderr_data.decode("utf-8", "replace").strip()
            if proc.returncode == 1:
                raise SystemAudioUnavailable(
                    "Audio Capture permission denied. Open System Settings → "
                    "Privacy & Security → Audio Capture and enable Passive Perception."
                )
            if proc.returncode == 2:
                raise SystemAudioUnavailable(
                    "macOS 14.2 or later is required for native system audio capture."
                )
            raise SystemAudioUnavailable(
                f"pp-system-audio exited before emitting header "
                f"(rc={proc.returncode}). stderr: {stderr_text}"
            )

        match = _HEADER_RE.match(header_line.rstrip(b"\n"))
        if not match:
            self._terminate_helper()
            raise SystemAudioUnavailable(f"pp-system-audio bad header: {header_line!r}")
        native_rate = int(match.group("rate"))
        channels = int(match.group("ch"))
        fmt = match.group("fmt").decode("ascii")
        if channels != 1 or fmt != "f32le":
            self._terminate_helper()
            raise SystemAudioUnavailable(
                f"pp-system-audio unexpected format: ch={channels} fmt={fmt}"
            )
        self._native_rate = native_rate
        g = gcd(self._target_rate, self._native_rate)
        self._up = self._target_rate // g
        self._down = self._native_rate // g
        logger.info(
            "[audio/macos-system] native=%dHz mono → resample to %dHz",
            self._native_rate, self._target_rate,
        )

        self._stop_event.clear()
        self._running = True

        self._reader_thread = threading.Thread(
            target=self._capture_loop,
            args=(callback,),
            name="pp-audio-macos-system",
            daemon=True,
        )
        self._reader_thread.start()

        self._stderr_thread = threading.Thread(
            target=self._drain_stderr,
            name="pp-audio-macos-system-stderr",
            daemon=True,
        )
        self._stderr_thread.start()

        # Watchdog — confirm audio actually flows. On macOS 15+/26 the tap
        # silently produces no buffers, so we have to verify by data, not by
        # API return codes. If nothing in 3s, tear down and let the caller
        # fall back to BlackHole.
        import time
        deadline = time.monotonic() + self._WATCHDOG_SECONDS
        while time.monotonic() < deadline:
            if self._bytes_received > 0:
                logger.info(
                    "[audio/macos-system] watchdog passed: %d bytes received within %.1fs",
                    self._bytes_received, self._WATCHDOG_SECONDS,
                )
                return
            time.sleep(0.1)
        logger.warning(
            "[audio/macos-system] watchdog timeout: 0 bytes in %.1fs — "
            "Process Tap is not delivering audio (known macOS 15+/26 issue). "
            "Tearing down so AudioCapture can fall back.",
            self._WATCHDOG_SECONDS,
        )
        # Best-effort teardown before raising.
        self._stop_event.set()
        self._running = False
        self._terminate_helper()
        raise SystemAudioUnavailable(
            f"Native system audio capture produced no data in {self._WATCHDOG_SECONDS}s. "
            "This is a known macOS 15+/26 issue with Core Audio process taps. "
            "Falling back to BlackHole."
        )

    def stop(self) -> None:
        self._stop_event.set()
        self._running = False
        self._terminate_helper()
        if self._reader_thread is not None:
            self._reader_thread.join(timeout=2.0)
            self._reader_thread = None
        if self._stderr_thread is not None:
            self._stderr_thread.join(timeout=1.0)
            self._stderr_thread = None
        self._helper_proc = None

    def list_devices(self) -> list[dict]:
        # The Process Tap is a single system-wide source. The device picker
        # surfaces this as a synthetic entry; the mic dropdown is unaffected.
        return [
            {
                "index": -1,
                "name": "System Audio",
                "channels": 1,
                "sample_rate": self._native_rate,
                "is_system": True,
            }
        ]

    def find_loopback_device(self) -> dict | None:
        return self.list_devices()[0]

    @property
    def sample_rate(self) -> int:
        return self._target_rate

    @property
    def is_running(self) -> bool:
        return self._running

    # ── Internals ─────────────────────────────────────────────────────────

    def _terminate_helper(self) -> None:
        proc = self._helper_proc
        if proc is None or proc.poll() is not None:
            return
        try:
            proc.terminate()
            proc.wait(timeout=2.0)
        except subprocess.TimeoutExpired:
            logger.warning("[audio/macos-system] helper did not exit, killing")
            proc.kill()
            try:
                proc.wait(timeout=1.0)
            except subprocess.TimeoutExpired:
                pass

    def _capture_loop(self, callback: Callable[[np.ndarray], None]) -> None:
        import time
        from scipy.signal import resample_poly

        assert self._helper_proc is not None and self._helper_proc.stdout is not None
        stdout = self._helper_proc.stdout
        bytes_per_read = _FRAMES_PER_READ * _BYTES_PER_SAMPLE
        # Heartbeat: log byte throughput every 10s so silent helper failures
        # are visible in launcher.log instead of just "No audio chunks".
        bytes_received = 0
        chunks_read = 0
        last_heartbeat = time.monotonic()
        last_bytes = 0
        try:
            while not self._stop_event.is_set():
                chunk = stdout.read(bytes_per_read)
                if not chunk:
                    logger.warning(
                        "[audio/macos-system] EOF from helper after %d bytes (%d reads)",
                        bytes_received, chunks_read,
                    )
                    break  # EOF — helper exited
                bytes_received += len(chunk)
                self._bytes_received = bytes_received  # for the start() watchdog
                chunks_read += 1
                now = time.monotonic()
                if now - last_heartbeat >= 10.0:
                    delta = bytes_received - last_bytes
                    if delta == 0:
                        logger.warning(
                            "[audio/macos-system] heartbeat: 0 bytes from helper in last 10s (total %d) — capture is stalled",
                            bytes_received,
                        )
                    else:
                        logger.info(
                            "[audio/macos-system] heartbeat: +%d bytes in 10s (total %d)",
                            delta, bytes_received,
                        )
                    last_heartbeat = now
                    last_bytes = bytes_received
                usable = len(chunk) - (len(chunk) % _BYTES_PER_SAMPLE)
                if usable <= 0:
                    continue
                samples = np.frombuffer(chunk[:usable], dtype=np.float32)
                if self._up != self._down:
                    samples = resample_poly(samples, self._up, self._down).astype(np.float32)
                else:
                    samples = samples.astype(np.float32, copy=False)
                callback(samples)
        except Exception as exc:
            logger.error("[audio/macos-system] capture loop failed: %s", exc)
        finally:
            self._running = False

    def _drain_stderr(self) -> None:
        proc = self._helper_proc
        if proc is None or proc.stderr is None:
            return
        for line in iter(proc.stderr.readline, b""):
            text = line.decode("utf-8", "replace").rstrip()
            if text:
                logger.info("[helper] %s", text)
