"""Audio utilities — concatenating chunk WAVs for the post-session pass."""

from __future__ import annotations

import logging
import wave
from pathlib import Path

import numpy as np

logger = logging.getLogger(__name__)


def concatenate_audio_chunks(
    chunk_paths: list[Path],
    output_path: Path,
    sample_rate: int = 16000,
) -> Path:
    """Concatenate a list of WAV chunks into a single WAV file."""
    all_samples = []
    for path in sorted(chunk_paths):
        if not path.exists():
            continue
        samples, _ = _load_wav_as_numpy(path)
        all_samples.append(samples)

    if not all_samples:
        raise RuntimeError("No audio chunks to concatenate")

    audio = np.concatenate(all_samples)
    pcm = (audio * 32767).astype(np.int16)

    with wave.open(str(output_path), "wb") as wf:
        wf.setnchannels(1)
        wf.setsampwidth(2)
        wf.setframerate(sample_rate)
        wf.writeframes(pcm.tobytes())

    duration = len(audio) / sample_rate
    logger.info("Concatenated %d chunks → %s (%.1fs)", len(chunk_paths), output_path.name, duration)
    return output_path


def _load_wav_as_numpy(wav_path: Path) -> tuple[np.ndarray, int]:
    with wave.open(str(wav_path), "rb") as wf:
        sr = wf.getframerate()
        n_frames = wf.getnframes()
        n_channels = wf.getnchannels()
        raw = wf.readframes(n_frames)
    pcm = np.frombuffer(raw, dtype=np.int16).astype(np.float32) / 32768.0
    if n_channels > 1:
        pcm = pcm.reshape(-1, n_channels).mean(axis=1)
    return pcm, sr
