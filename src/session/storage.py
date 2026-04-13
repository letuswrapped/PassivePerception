"""Save session transcript and notes to markdown files."""

from __future__ import annotations

import os
import tempfile
from pathlib import Path

from src.notes.models import SessionNotes
from src.transcription.diarization import TranscriptLine


def save_session(
    session_dir: Path,
    transcript: list[TranscriptLine],
    notes: SessionNotes,
    auto_delete_audio: bool = True,
    tmp_dir: Path | None = None,
) -> Path:
    """
    Write transcript.md and notes.md to session_dir.
    Optionally delete all WAV files from tmp_dir.
    Returns session_dir.
    """
    _write_transcript(session_dir / "transcript.md", transcript)
    _write_notes(session_dir / "notes.md", notes)

    if auto_delete_audio and tmp_dir and tmp_dir.exists():
        for wav in tmp_dir.glob("*.wav"):
            wav.unlink(missing_ok=True)

    return session_dir


def _atomic_write(path: Path, content: str) -> None:
    """Write to a temp file then atomically rename — crash-safe."""
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(dir=path.parent, suffix=".tmp")
    try:
        os.write(fd, content.encode("utf-8"))
        os.fsync(fd)
        os.close(fd)
        os.replace(tmp, path)
    except BaseException:
        try:
            os.close(fd)
        except OSError:
            pass
        Path(tmp).unlink(missing_ok=True)
        raise


def _write_transcript_str(transcript: list[TranscriptLine]) -> str:
    """Format transcript as markdown string."""
    lines = ["# Session Transcript\n"]
    current_speaker = None
    for line in transcript:
        if line.speaker_label != current_speaker:
            lines.append(f"\n**{line.speaker_label}** `{_fmt_time(line.start)}`  ")
            current_speaker = line.speaker_label
        else:
            lines.append(f"`{_fmt_time(line.start)}`  ")
        lines.append(line.text + "\n")
    return "\n".join(lines)


def _write_transcript(path: Path, transcript: list[TranscriptLine]) -> None:
    _atomic_write(path, _write_transcript_str(transcript))


def _write_notes_str(notes: SessionNotes) -> str:
    """Format notes as markdown string."""
    parts = ["# Session Notes\n"]

    if notes.summary:
        parts.append("## Summary\n")
        parts.append(notes.summary + "\n")

    if notes.npcs:
        parts.append("\n## NPCs\n")
        for npc in notes.npcs:
            parts.append(f"\n### {npc.name}")
            if npc.relationship:
                parts.append(f"*{npc.relationship}*")
            if npc.description:
                parts.append(npc.description)
            if npc.notes:
                parts.append(f"> {npc.notes}")

    if notes.locations:
        parts.append("\n## Locations\n")
        for loc in notes.locations:
            parts.append(f"\n### {loc.name}")
            if loc.description:
                parts.append(loc.description)
            if loc.significance:
                parts.append(f"*{loc.significance}*")

    if notes.plot_points:
        parts.append("\n## Plot Points\n")
        for i, pp in enumerate(notes.plot_points, 1):
            parts.append(f"\n{i}. **{pp.summary}**")
            if pp.npcs_involved:
                parts.append(f"   *NPCs: {', '.join(pp.npcs_involved)}*")
            if pp.context:
                parts.append(f"   {pp.context}")

    if notes.open_questions:
        parts.append("\n## Open Questions\n")
        for q in notes.open_questions:
            parts.append(f"- {q}")

    return "\n".join(parts)


def _write_notes(path: Path, notes: SessionNotes) -> None:
    _atomic_write(path, _write_notes_str(notes))


def _fmt_time(seconds: float) -> str:
    m, s = divmod(int(seconds), 60)
    h, m = divmod(m, 60)
    if h:
        return f"{h}:{m:02d}:{s:02d}"
    return f"{m:02d}:{s:02d}"
