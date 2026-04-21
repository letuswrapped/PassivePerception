"""Transcription — cloud-based via Deepgram Nova-3."""

from dataclasses import dataclass


@dataclass
class TranscriptLine:
    """A single labeled line in the saved transcript."""
    start: float
    end: float
    speaker_id: str
    speaker_label: str
    text: str


def default_speaker_label(speaker_id: str) -> str:
    """Derive a human label from a speaker id like 'SPEAKER_02' → 'Speaker 3'."""
    try:
        idx = int(speaker_id.split("_")[-1])
        return f"Speaker {idx + 1}"
    except (ValueError, IndexError):
        return speaker_id


__all__ = ["TranscriptLine", "default_speaker_label"]
