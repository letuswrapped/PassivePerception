"""
Audio buffer that accumulates PCM frames into fixed-duration chunks
and writes them as WAV files for downstream transcription.
"""

from __future__ import annotations

import asyncio
import queue
import threading
import wave
from datetime import datetime
from pathlib import Path

import numpy as np


class AudioBuffer:
    """
    Receives raw float32 PCM samples from AudioCapture and accumulates them.
    When a full chunk (default 30 s) is ready it writes a WAV file and puts
    the file path on an asyncio Queue for the transcription pipeline to consume.

    Usage:
        buffer = AudioBuffer(sample_rate=16000, chunk_duration=30, tmp_dir=Path("tmp"))
        buffer.set_output_queue(chunk_queue)   # asyncio.Queue[Path]
        capture.add_callback(buffer.feed)
        buffer.start()
        ...
        buffer.stop()                          # flushes any partial chunk
    """

    def __init__(
        self,
        sample_rate: int = 16000,
        chunk_duration: int = 30,
        tmp_dir: Path = Path("tmp"),
    ) -> None:
        self._sample_rate = sample_rate
        self._chunk_samples = sample_rate * chunk_duration
        self._tmp_dir = tmp_dir
        self._tmp_dir.mkdir(parents=True, exist_ok=True)

        self._buffer: list[np.ndarray] = []
        self._buffered_samples = 0
        self._chunk_index = 0
        self._lock = threading.Lock()
        self._output_queue: asyncio.Queue | None = None
        self._loop: asyncio.AbstractEventLoop | None = None
        self._running = False

    def set_output_queue(
        self,
        q: asyncio.Queue,
        loop: asyncio.AbstractEventLoop,
    ) -> None:
        self._output_queue = q
        self._loop = loop

    def start(self) -> None:
        self._running = True

    def stop(self) -> None:
        """Stop the buffer and flush any remaining audio as a final chunk."""
        self._running = False
        with self._lock:
            if self._buffered_samples > 0:
                self._flush()

    def feed(self, samples: np.ndarray) -> None:
        """Receive samples from the audio capture callback (called from audio thread)."""
        if not self._running:
            return
        with self._lock:
            self._buffer.append(samples)
            self._buffered_samples += len(samples)
            if self._buffered_samples >= self._chunk_samples:
                self._flush()

    def _flush(self) -> None:
        """Write accumulated samples to a WAV file and enqueue the path."""
        audio = np.concatenate(self._buffer)
        # Only take exactly one chunk's worth; keep the remainder
        chunk = audio[: self._chunk_samples]
        remainder = audio[self._chunk_samples :]

        self._buffer = [remainder] if len(remainder) > 0 else []
        self._buffered_samples = len(remainder)

        path = self._write_wav(chunk)
        if self._output_queue is not None and self._loop is not None:
            asyncio.run_coroutine_threadsafe(
                self._output_queue.put(path), self._loop
            )

    def _write_wav(self, samples: np.ndarray) -> Path:
        timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
        path = self._tmp_dir / f"chunk_{self._chunk_index:04d}_{timestamp}.wav"
        self._chunk_index += 1

        pcm_int16 = (samples * 32767).astype(np.int16)
        with wave.open(str(path), "wb") as wf:
            wf.setnchannels(1)
            wf.setsampwidth(2)  # int16 = 2 bytes
            wf.setframerate(self._sample_rate)
            wf.writeframes(pcm_int16.tobytes())

        return path
