"""Fallback transcription backend — faster-whisper on CPU or CUDA."""

from __future__ import annotations

from pathlib import Path

from src.transcription.backends.base import Segment, TranscriptionBackend


class WhisperTranscriptionBackend(TranscriptionBackend):
    """
    Transcription via faster-whisper using ONNX Runtime.

    Used on:
      - Windows (any hardware)
      - macOS Intel
      - Any system without Apple Silicon

    On NVIDIA GPUs, set device="cuda" in config for full GPU acceleration.
    """

    def __init__(
        self,
        model_name: str = "small.en",
        language: str = "en",
        device: str = "cpu",
        compute_type: str = "int8",
    ) -> None:
        self._model_name = model_name
        self._language = language
        self._device = device
        self._compute_type = compute_type
        self._model = None

    def load(self) -> None:
        from faster_whisper import WhisperModel
        self._model = WhisperModel(
            self._model_name,
            device=self._device,
            compute_type=self._compute_type,
        )
        print(f"[transcription/whisper] Model ready: {self._model_name} on {self._device}")

    def transcribe(self, wav_path: Path) -> list[Segment]:
        if self._model is None:
            self.load()

        raw_segments, info = self._model.transcribe(
            str(wav_path),
            language=self._language,
            beam_size=5,
            vad_filter=True,
            vad_parameters={
                "threshold": 0.3,
                "min_silence_duration_ms": 300,
                "speech_pad_ms": 400,
            },
        )

        segments = []
        for s in raw_segments:
            text = s.text.strip()
            if text:
                segments.append(Segment(start=s.start, end=s.end, text=text))

        print(f"[transcription/whisper] {wav_path.name}: {len(segments)} segments")
        for s in segments:
            print(f"  [{s.start:.1f}→{s.end:.1f}] {s.text}")

        return segments
