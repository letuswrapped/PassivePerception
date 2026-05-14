"""
Dual-source mixing invariant.

CLAUDE.md hard rule: "preserve the thread-safe buffer + clipping in
AudioCapture. Length mismatches between streams are expected."

This test drives the mixing closure in src/audio/capture.py::AudioCapture
deterministically — no real backends, no sleeping. We push 10 s worth of
synthetic frames (1000 iterations × 10 ms) from both primary and mic at
0.9 amplitude. The unclipped sum would peak at 1.8; the contract is that
every emitted frame stays in [-1.0, 1.0].

Run: `python tests/test_audio_mixing.py` (no pytest dependency required).
"""

from __future__ import annotations

import sys
import unittest
from collections.abc import Callable
from pathlib import Path

import numpy as np

# Allow `from src.audio...` when invoked as a standalone script from any cwd.
_REPO = Path(__file__).resolve().parent.parent
if str(_REPO) not in sys.path:
    sys.path.insert(0, str(_REPO))

import src.audio.capture as capture_mod  # noqa: E402
from src.audio.backends.base import AudioCaptureBackend  # noqa: E402
from src.audio.capture import AudioCapture  # noqa: E402


class _CallbackCapture(AudioCaptureBackend):
    """Backend stub that just records the callback AudioCapture installs."""

    def __init__(self, **_: object) -> None:
        self.cb: Callable[[np.ndarray], None] | None = None
        self._device_name = "fake"
        self._running = False

    def start(self, callback: Callable[[np.ndarray], None]) -> None:
        self.cb = callback
        self._running = True

    def stop(self) -> None:
        self._running = False

    def list_devices(self) -> list[dict]:
        return []

    def find_loopback_device(self) -> dict | None:
        return None

    @property
    def sample_rate(self) -> int:
        return 16000

    @property
    def is_running(self) -> bool:
        return self._running


class DualSourceMixingTest(unittest.TestCase):
    def test_clipping_invariant_holds_over_10s(self) -> None:
        primary = _CallbackCapture()
        mic = _CallbackCapture()
        backends = iter([primary, mic])
        original_make = capture_mod._make_backend
        capture_mod._make_backend = lambda name, rate: next(backends)
        try:
            cap = AudioCapture(device_name="fake-primary")
            cap.set_mic_device("fake-mic")
            received: list[np.ndarray] = []
            cap.add_callback(received.append)
            cap.start()
            self.assertIsNotNone(primary.cb, "primary callback never installed")
            self.assertIsNotNone(mic.cb, "mic callback never installed")
            assert primary.cb is not None and mic.cb is not None  # for type checker

            # 10 s × 100 callbacks/s × 160 samples = 1000 iterations of 10 ms
            # frames at 16 kHz. Both streams at 0.9 amp → unclipped sum 1.8.
            frame = np.full(160, 0.9, dtype=np.float32)
            for _ in range(1000):
                mic.cb(frame)       # populates _mic_buffer under lock
                primary.cb(frame)   # triggers _on_primary → mix → clip → callback

            cap.stop()
        finally:
            capture_mod._make_backend = original_make

        self.assertGreater(len(received), 0, "callback never fired")
        for i, arr in enumerate(received):
            peak = float(np.max(np.abs(arr)))
            self.assertLessEqual(
                peak, 1.0,
                f"clipping invariant violated at frame {i}: peak={peak}",
            )

        # Sanity: most frames should be at or very near the clip ceiling
        # (so the test is exercising the mix path, not just passing primary
        # through unchanged).
        near_clip = sum(1 for arr in received if float(np.max(np.abs(arr))) >= 0.95)
        self.assertGreater(
            near_clip, len(received) // 2,
            f"expected most frames near-clipping; got {near_clip}/{len(received)}",
        )


if __name__ == "__main__":
    unittest.main()
