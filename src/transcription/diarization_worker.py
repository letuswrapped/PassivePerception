"""
Diarization worker — tries FluidAudio Swift CLI first (Apple Neural Engine),
falls back to simple-diarizer (CPU-based ECAPA-TDNN) on other platforms.

No HuggingFace token or cloud API required for either path.
Runs in a subprocess so all memory is freed when the worker exits.

Usage:
    python -m src.transcription.diarization_worker \
        --audio path/to/session_full.wav \
        --out   path/to/segments.json \
        [--num-speakers 0]

Output format:
    {"segments": [{"start": 0.0, "end": 5.0, "speaker": "SPEAKER_00"}, ...]}
"""

from __future__ import annotations

import argparse
import json
import platform
import shutil
import subprocess
import sys
import warnings
from pathlib import Path

# Suppress noisy warnings
warnings.filterwarnings("ignore", message="Module 'speechbrain.pretrained'")

# Location of the Swift CLI binary (built from swift-diarizer/)
_SWIFT_CLI_PATHS = [
    Path(__file__).parent.parent.parent / "swift-diarizer" / ".build" / "release" / "DiarizeCLI",
    Path(__file__).parent.parent.parent / "build" / "diarize-cli",
    # Bundled in .app
    Path(__file__).parent.parent.parent / "diarize-cli",
]


def _find_swift_cli() -> Path | None:
    """Find the FluidAudio Swift CLI binary."""
    for p in _SWIFT_CLI_PATHS:
        if p.exists() and p.is_file():
            return p
    # Also check PATH
    which = shutil.which("diarize-cli")
    if which:
        return Path(which)
    return None


def _run_swift_diarization(
    audio_path: Path,
    out_path: Path,
    num_speakers: int,
    log,
) -> bool:
    """Try FluidAudio Swift CLI. Returns True if successful."""
    if platform.system() != "Darwin":
        return False

    cli = _find_swift_cli()
    if cli is None:
        log("FluidAudio Swift CLI not found — will use Python fallback")
        return False

    log(f"Using FluidAudio (Apple Neural Engine) via {cli.name}")

    cmd = [str(cli), str(audio_path), str(out_path)]
    if num_speakers > 0:
        cmd += ["--num-speakers", str(num_speakers)]

    try:
        proc = subprocess.Popen(
            cmd,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            text=True,
        )
        for line in proc.stdout:
            line = line.rstrip()
            if line:
                print(line, flush=True)

        proc.wait(timeout=1800)

        if proc.returncode == 0 and out_path.exists():
            return True
        else:
            log(f"Swift CLI exited with code {proc.returncode}")
            return False

    except subprocess.TimeoutExpired:
        proc.kill()
        proc.wait()
        log("Swift CLI timed out after 30 minutes")
        return False
    except Exception as exc:
        log(f"Swift CLI failed: {exc}")
        return False


def _run_python_diarization(
    audio_path: Path,
    out_path: Path,
    num_speakers: int,
    log,
) -> None:
    """Fallback: simple-diarizer (ECAPA-TDNN + spectral clustering)."""
    import torch
    # Trust the Silero VAD repo so it downloads without an interactive prompt
    torch.hub._validate_not_a_forked_repo = lambda *a, **k: True  # noqa
    torch.hub._check_repo_is_trusted = lambda *a, **k: None  # noqa

    from simple_diarizer.diarizer import Diarizer

    log("Loading ECAPA-TDNN speaker embedding model...")
    diar = Diarizer(embed_model='ecapa')

    n = num_speakers if num_speakers > 0 else None
    log(f"Diarizing (speakers={'auto' if n is None else n})...")

    try:
        raw_segments = diar.diarize(
            str(audio_path),
            num_speakers=n,
        )
    except AssertionError as e:
        if "speech" in str(e).lower() or "VAD" in str(e):
            log("No speech detected in audio — skipping diarization")
            out_path.write_text(json.dumps({"segments": []}))
            return
        raise

    # Convert to our segment format
    segments = []
    for seg in raw_segments:
        segments.append({
            "start": round(float(seg["start"]), 3),
            "end": round(float(seg["end"]), 3),
            "speaker": f"SPEAKER_{int(seg['label']):02d}",
        })

    speakers = sorted(set(s["speaker"] for s in segments))
    log(f"Found {len(speakers)} speaker(s): {speakers}")
    log(f"Writing {len(segments)} segments to {out_path.name}")

    out_path.write_text(json.dumps({"segments": segments}))


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--audio", required=True)
    parser.add_argument("--out", required=True)
    parser.add_argument("--num-speakers", type=int, default=0,
                        help="Expected number of speakers (0 = auto-detect)")
    parser.add_argument("--threshold", type=float, default=None,
                        help="(Legacy, ignored)")
    args = parser.parse_args()

    audio_path = Path(args.audio)
    out_path = Path(args.out)

    def log(msg: str) -> None:
        print(f"[diarization] {msg}", flush=True)

    if not audio_path.exists():
        log(f"Audio file not found: {audio_path}")
        out_path.write_text(json.dumps({"error": "Audio not found", "segments": []}))
        sys.exit(1)

    size_mb = audio_path.stat().st_size / 1024 / 1024
    log(f"Running diarization on {audio_path.name} ({size_mb:.1f} MB)...")

    # Try FluidAudio Swift CLI first (macOS + Apple Neural Engine)
    if _run_swift_diarization(audio_path, out_path, args.num_speakers, log):
        log("Diarization complete (FluidAudio/Neural Engine)")
        return

    # Fallback: simple-diarizer (Python, CPU-based, cross-platform)
    try:
        log("Using simple-diarizer (CPU fallback)...")
        _run_python_diarization(audio_path, out_path, args.num_speakers, log)
        log("Diarization complete (simple-diarizer)")
    except ImportError:
        log("simple-diarizer not installed. Run: pip install simple-diarizer")
        out_path.write_text(json.dumps({
            "error": "No diarization backend available",
            "segments": [],
        }))
        sys.exit(1)
    except Exception as exc:
        log(f"Diarization failed: {exc}")
        out_path.write_text(json.dumps({
            "error": str(exc),
            "segments": [],
        }))
        sys.exit(1)


if __name__ == "__main__":
    main()
