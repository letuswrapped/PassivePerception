"""
Deepgram transcription client — replaces the local MLX/faster-whisper engine
and the separate diarization step with a single cloud API call.

Two call shapes used by the session manager:

  transcribe_preview(wav_path, keyterms)
    Fast, no diarization. Used during the live 15-min notes loop — we only
    need the text, speakers get resolved in the post-session pass.

  transcribe_full(wav_path, keyterms)
    Full Nova-3 pass with diarization. Used once post-session on the
    concatenated WAV to produce the canonical speaker-labeled transcript.

Both enable `mip_opt_out=True` so the audio is not retained for model
training (Deepgram's zero-retention knob on the paid tier). Keyterms bias
Nova-3 toward the character/NPC/location names from the active campaign —
this is what keeps fantasy proper nouns from being garbled.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from pathlib import Path
from typing import Optional

from deepgram import DeepgramClient

from src import cloud_config

logger = logging.getLogger(__name__)


@dataclass
class TranscribedWord:
    start: float
    end: float
    text: str
    speaker: Optional[int] = None   # Deepgram returns 0-indexed ints when diarize=True


@dataclass
class TranscribedUtterance:
    """A contiguous block of speech from a single speaker."""
    start: float
    end: float
    text: str
    speaker: Optional[int] = None


@dataclass
class TranscriptionResult:
    utterances: list[TranscribedUtterance]
    full_text: str


class DeepgramError(RuntimeError):
    pass


def _client() -> DeepgramClient:
    key = cloud_config.get_deepgram_key()
    if not key:
        raise DeepgramError("No Deepgram API key configured. Add one in Settings → API Keys.")
    return DeepgramClient(api_key=key)


def _read_audio(wav_path: Path) -> bytes:
    if not wav_path.exists():
        raise DeepgramError(f"Audio file not found: {wav_path}")
    return wav_path.read_bytes()


def _utterances_from_response(response) -> list[TranscribedUtterance]:
    """
    Build utterance-grouped output from a Deepgram response.

    Prefers the native `utterances` array when present; otherwise groups
    word-level output by contiguous speaker. All timestamps are relative
    to the start of the submitted audio.
    """
    try:
        utt_list = getattr(response.results, "utterances", None)
    except AttributeError:
        utt_list = None

    if utt_list:
        out: list[TranscribedUtterance] = []
        for u in utt_list:
            out.append(TranscribedUtterance(
                start=float(getattr(u, "start", 0.0) or 0.0),
                end=float(getattr(u, "end", 0.0) or 0.0),
                text=(getattr(u, "transcript", "") or "").strip(),
                speaker=getattr(u, "speaker", None),
            ))
        return [u for u in out if u.text]

    # Fall back to word-level grouping
    try:
        alt = response.results.channels[0].alternatives[0]
        words = getattr(alt, "words", []) or []
    except (AttributeError, IndexError):
        words = []

    grouped: list[TranscribedUtterance] = []
    current: Optional[TranscribedUtterance] = None
    for w in words:
        spk = getattr(w, "speaker", None)
        text = getattr(w, "punctuated_word", None) or getattr(w, "word", "") or ""
        start = float(getattr(w, "start", 0.0) or 0.0)
        end = float(getattr(w, "end", 0.0) or 0.0)
        if current is None or current.speaker != spk:
            if current is not None and current.text:
                grouped.append(current)
            current = TranscribedUtterance(start=start, end=end, text=text, speaker=spk)
        else:
            current.text = (current.text + " " + text).strip()
            current.end = end
    if current is not None and current.text:
        grouped.append(current)
    return grouped


def _full_text(utterances: list[TranscribedUtterance]) -> str:
    return " ".join(u.text for u in utterances if u.text).strip()


def _common_params(keyterms: list[str]) -> dict:
    """Parameters shared across preview + full passes."""
    params: dict = {
        "model": "nova-3",
        "language": "en",
        "smart_format": True,
        "punctuate": True,
        "mip_opt_out": True,   # opt out of Deepgram's Model Improvement Program (no retention for training)
    }
    if keyterms:
        # Nova-3 supports multi-word keyterm boosting. SDK accepts list via repeated keyterm= query params.
        params["keyterm"] = keyterms[:100]   # Deepgram caps keyterm count; trim to stay safe
    return params


def transcribe_preview(wav_path: Path, keyterms: Optional[list[str]] = None) -> TranscriptionResult:
    """
    Fast transcription for the live notes loop. No diarization.
    Sends the single WAV file and returns text utterances.
    """
    audio = _read_audio(wav_path)
    client = _client()
    params = _common_params(keyterms or [])
    # No diarization on preview — speakers will be resolved in the post-session full pass
    response = client.listen.v1.media.transcribe_file(request=audio, **params)
    utterances = _utterances_from_response(response)
    return TranscriptionResult(utterances=utterances, full_text=_full_text(utterances))


def transcribe_full(wav_path: Path, keyterms: Optional[list[str]] = None) -> TranscriptionResult:
    """
    Canonical post-session pass. Diarization on. Used on the full concatenated WAV.
    """
    audio = _read_audio(wav_path)
    client = _client()
    params = _common_params(keyterms or [])
    params["diarize"] = True
    params["utterances"] = True   # request grouped-by-speaker output
    response = client.listen.v1.media.transcribe_file(request=audio, **params)
    utterances = _utterances_from_response(response)
    return TranscriptionResult(utterances=utterances, full_text=_full_text(utterances))
