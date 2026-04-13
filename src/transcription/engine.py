"""
Transcription engine — thin platform wrapper.

Auto-selects the correct backend:
  - Apple Silicon (Darwin + arm64) → mlx-whisper on Neural Engine
  - Everything else                → faster-whisper on CPU/CUDA
"""

from __future__ import annotations

import platform
import sys
from pathlib import Path

from src.transcription.backends.base import Segment, TranscriptionBackend


def _make_backend(model_name: str, language: str, device: str) -> TranscriptionBackend:
    is_apple_silicon = (
        platform.system() == "Darwin"
        and platform.machine() == "arm64"
    )

    if is_apple_silicon:
        try:
            import mlx_whisper  # noqa: F401
            from src.transcription.backends.mlx_backend import MLXTranscriptionBackend
            print(f"[transcription] Using MLX backend (Apple Silicon Neural Engine)")
            return MLXTranscriptionBackend(model_name=model_name, language=language)
        except ImportError:
            print("[transcription] mlx-whisper not installed, falling back to faster-whisper")

    from src.transcription.backends.whisper_backend import WhisperTranscriptionBackend
    print(f"[transcription] Using faster-whisper backend ({device})")
    return WhisperTranscriptionBackend(
        model_name=model_name,
        language=language,
        device=device,
    )


class TranscriptionEngine:
    """Platform-agnostic transcription. Delegates to the appropriate backend."""

    def __init__(
        self,
        model_name: str = "small.en",
        language: str = "en",
        device: str = "cpu",
    ) -> None:
        self._backend = _make_backend(model_name, language, device)

    def transcribe(self, wav_path: Path) -> list[Segment]:
        return self._backend.transcribe(wav_path)

    def load(self) -> None:
        self._backend.load()
