"""Apple Silicon transcription backend — mlx-whisper on the Neural Engine."""

from __future__ import annotations

from pathlib import Path

from src.transcription.backends.base import Segment, TranscriptionBackend

# MLX model repos on HuggingFace (mlx-community)
MLX_MODELS = {
    "tiny.en":   "mlx-community/whisper-tiny.en-mlx",
    "base.en":   "mlx-community/whisper-base.en-mlx",
    "small.en":  "mlx-community/whisper-small.en-mlx",
    "medium.en": "mlx-community/whisper-medium.en-mlx",
    "large-v3":  "mlx-community/whisper-large-v3-mlx",
}


class MLXTranscriptionBackend(TranscriptionBackend):
    """
    Transcription via mlx-whisper running on the Apple Neural Engine.

    Typically 3-5x faster than CPU-based faster-whisper on M-series chips.
    Model is downloaded from HuggingFace on first use and cached locally.
    """

    def __init__(self, model_name: str = "small.en", language: str = "en") -> None:
        self._model_name = model_name
        self._language = language
        self._repo = MLX_MODELS.get(model_name, model_name)
        self._loaded = False

    def load(self) -> None:
        """Pre-load the model (optional — transcribe() lazy-loads if not called)."""
        import mlx_whisper  # noqa: F401 — triggers download if needed
        self._loaded = True
        print(f"[transcription/mlx] Model ready: {self._repo}")

    def transcribe(self, wav_path: Path) -> list[Segment]:
        import mlx_whisper

        result = mlx_whisper.transcribe(
            str(wav_path),
            path_or_hf_repo=self._repo,
            language=self._language,
            word_timestamps=False,
            verbose=False,
        )

        segments = []
        for seg in result.get("segments", []):
            text = seg.get("text", "").strip()
            if text:
                segments.append(Segment(
                    start=float(seg["start"]),
                    end=float(seg["end"]),
                    text=text,
                ))

        print(f"[transcription/mlx] {wav_path.name}: {len(segments)} segments")
        for s in segments:
            print(f"  [{s.start:.1f}→{s.end:.1f}] {s.text}")

        return segments
