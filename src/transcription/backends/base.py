"""Abstract base class for transcription backends."""

from __future__ import annotations

from abc import ABC, abstractmethod
from dataclasses import dataclass
from pathlib import Path


@dataclass
class Segment:
    start: float   # seconds from chunk start
    end: float
    text: str


class TranscriptionBackend(ABC):
    """
    Platform-specific transcription backend.

    Implementations:
      - mlx_backend.py     — mlx-whisper on Apple Silicon Neural Engine
      - whisper_backend.py — faster-whisper on CPU/CUDA (Windows / fallback)
    """

    @abstractmethod
    def transcribe(self, wav_path: Path) -> list[Segment]:
        """Transcribe a WAV file and return timestamped segments."""
        ...

    @abstractmethod
    def load(self) -> None:
        """Load the model into memory (called once before first transcription)."""
        ...
