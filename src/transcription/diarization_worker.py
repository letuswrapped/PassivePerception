"""
Diarization worker — runs as a subprocess so all PyTorch/pyannote memory
is released immediately when this process exits (no lingering in the parent).

Usage:
    python -m src.transcription.diarization_worker \
        --audio path/to/session_full.wav \
        --out   path/to/segments.json \
        --model pyannote/speaker-diarization-3.1 \
        --token hf_xxxx
"""

from __future__ import annotations

import argparse
import json
import sys
import wave
from pathlib import Path

import numpy as np
import torch


def _best_device() -> str:
    # CPU only on Apple Silicon — MPS causes 5-6 GB shared-memory spikes.
    # CUDA is fine because it has dedicated VRAM.
    if torch.cuda.is_available():
        return "cuda"
    return "cpu"


def _load_wav(wav_path: Path):
    with wave.open(str(wav_path), "rb") as wf:
        sr = wf.getframerate()
        n_frames = wf.getnframes()
        n_channels = wf.getnchannels()
        raw = wf.readframes(n_frames)
    pcm = np.frombuffer(raw, dtype=np.int16).astype(np.float32) / 32768.0
    if n_channels > 1:
        pcm = pcm.reshape(-1, n_channels).mean(axis=1)
    return torch.from_numpy(pcm).unsqueeze(0), sr


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--audio",        required=True)
    parser.add_argument("--out",          required=True)
    parser.add_argument("--model",        default="pyannote/speaker-diarization-3.1")
    parser.add_argument("--token",        required=True)
    parser.add_argument("--min-speakers", type=int, default=None)
    parser.add_argument("--max-speakers", type=int, default=None)
    args = parser.parse_args()

    audio_path = Path(args.audio)
    out_path   = Path(args.out)
    device_str = _best_device()

    def log(msg: str) -> None:
        print(f"[diarization] {msg}", flush=True)

    log(f"Loading pyannote pipeline on {device_str}...")
    try:
        from pyannote.audio import Pipeline
        pipeline = Pipeline.from_pretrained(args.model, token=args.token)
        pipeline.to(torch.device(device_str))
    except Exception as exc:
        log(f"Failed to load pipeline: {exc}")
        out_path.write_text(json.dumps({"error": str(exc), "segments": []}))
        sys.exit(1)

    size_mb = audio_path.stat().st_size / 1024 / 1024
    speaker_hint = ""
    if args.min_speakers or args.max_speakers:
        speaker_hint = f" (speakers: {args.min_speakers}–{args.max_speakers})"
    log(f"Running diarization on {audio_path.name} ({size_mb:.1f} MB){speaker_hint}...")

    try:
        waveform, sample_rate = _load_wav(audio_path)
        diar_kwargs = {}
        if args.min_speakers is not None:
            diar_kwargs["min_speakers"] = args.min_speakers
        if args.max_speakers is not None:
            diar_kwargs["max_speakers"] = args.max_speakers
        diarize_output = pipeline({"waveform": waveform, "sample_rate": sample_rate}, **diar_kwargs)
        annotation = diarize_output

        segments = []
        for turn, _, speaker in annotation.itertracks(yield_label=True):
            segments.append({
                "start":   round(turn.start, 3),
                "end":     round(turn.end,   3),
                "speaker": speaker,
            })

        speakers = list({s["speaker"] for s in segments})
        log(f"Found {len(speakers)} speaker(s): {speakers}")
        log(f"Writing {len(segments)} segments to {out_path.name}")

        out_path.write_text(json.dumps({"segments": segments}))

    except Exception as exc:
        log(f"Diarization failed: {exc}")
        out_path.write_text(json.dumps({"error": str(exc), "segments": []}))
        sys.exit(1)

    # Process exits here — OS reclaims all PyTorch/pyannote memory immediately.


if __name__ == "__main__":
    main()
