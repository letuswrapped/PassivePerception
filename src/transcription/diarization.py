"""
Post-session speaker diarization using simple-diarizer (ECAPA-TDNN).

Runs ONCE after the session ends on the full concatenated audio.
Fully local — no HuggingFace token or cloud API required.
Executes in a subprocess so all memory is freed immediately when
the worker process exits — no lingering in the main app.
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
import wave
from dataclasses import dataclass
from pathlib import Path
from typing import Callable

import numpy as np


@dataclass
class TranscriptLine:
    """A single labeled line in the running transcript."""
    start: float
    end: float
    speaker_id: str
    speaker_label: str
    text: str


def _default_label(speaker_id: str) -> str:
    try:
        idx = int(speaker_id.split("_")[-1])
        return f"Speaker {idx + 1}"
    except (ValueError, IndexError):
        return speaker_id


def run_diarization(
    audio_path: Path,
    transcript_lines: list[TranscriptLine],
    speaker_labels: dict[str, str],
    progress_cb: Callable[[str], None] | None = None,
    threshold: float = 0.8,
    **_kwargs,
) -> list[TranscriptLine]:
    """
    Run speaker diarization via simple-diarizer (ECAPA-TDNN embeddings),
    then apply the resulting speaker segments back to transcript_lines.

    No HuggingFace token required. Models auto-download on first run.

    The subprocess approach ensures all memory is released by the OS
    the moment the worker exits — no lingering in the main app.
    """
    def _progress(msg: str) -> None:
        print(f"[diarization] {msg}", flush=True)
        if progress_cb:
            progress_cb(msg)

    # Write segments to a temp JSON file next to the audio
    segments_path = audio_path.with_suffix(".segments.json")

    _progress("Starting diarization worker...")
    cmd = [
        sys.executable, "-m", "src.transcription.diarization_worker",
        "--audio", str(audio_path),
        "--out",   str(segments_path),
    ]

    try:
        # Stream the worker's stdout so progress messages appear in the terminal
        proc = subprocess.Popen(
            cmd,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            text=True,
            cwd=Path(__file__).parent.parent.parent,  # repo root
        )
        for line in proc.stdout:
            line = line.rstrip()
            if line:
                # Worker already prefixes with [diarization]; just forward it
                print(line, flush=True)
                # Surface key messages to the UI via progress_cb
                if progress_cb and "[diarization]" in line:
                    progress_cb(line.split("[diarization]", 1)[-1].strip())

        proc.wait(timeout=1800)  # 30-minute timeout for diarization
        # Worker process has exited — OS has freed its memory
        _progress("Worker process exited. Applying speaker labels...")

    except subprocess.TimeoutExpired:
        proc.kill()
        proc.wait()
        _progress("Diarization timed out after 30 minutes — using unlabeled transcript")
        return transcript_lines
    except Exception as exc:
        _progress(f"Failed to launch diarization worker: {exc}")
        return transcript_lines

    # Read results
    if not segments_path.exists():
        _progress("No output from worker — skipping speaker labeling")
        return transcript_lines

    try:
        data = json.loads(segments_path.read_text())
        segments_path.unlink(missing_ok=True)

        if "error" in data:
            _progress(f"Worker reported error: {data['error']}")
            return transcript_lines

        segments = data.get("segments", [])
        if not segments:
            _progress("No diarization segments returned")
            return transcript_lines

        speakers = list({s["speaker"] for s in segments})
        _progress(f"Applying {len(speakers)} speaker(s) to {len(transcript_lines)} lines")

        updated = 0
        for line in transcript_lines:
            new_id = _find_speaker(line.start, line.end, segments)
            if new_id != line.speaker_id:
                line.speaker_id = new_id
                updated += 1
            line.speaker_label = speaker_labels.get(
                line.speaker_id, _default_label(line.speaker_id)
            )
        _progress(f"Done — {updated} lines relabeled")

    except Exception as exc:
        _progress(f"Failed to apply diarization results: {exc}")

    return transcript_lines


def concatenate_audio_chunks(
    chunk_paths: list[Path],
    output_path: Path,
    sample_rate: int = 16000,
) -> Path:
    """
    Concatenate a list of WAV chunks into a single WAV file for diarization.
    Returns output_path.
    """
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
    print(f"[diarization] Concatenated {len(chunk_paths)} chunks → "
          f"{output_path.name} ({duration:.1f}s)")
    return output_path


def _find_speaker(start: float, end: float, segments: list[dict]) -> str:
    best_speaker = "SPEAKER_00"
    best_overlap = 0.0
    for d in segments:
        overlap = max(0.0, min(end, d["end"]) - max(start, d["start"]))
        if overlap > best_overlap:
            best_overlap = overlap
            best_speaker = d["speaker"]
    return best_speaker


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
