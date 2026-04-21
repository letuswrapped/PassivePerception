"""Save session transcript and notes to markdown files."""

from __future__ import annotations

import json
import logging
import os
import tempfile
from datetime import datetime
from pathlib import Path

from src.notes.models import Pass1Result, SessionNotes
from src.transcription import TranscriptLine

logger = logging.getLogger(__name__)


# ── Pass 1 artifact helpers ──────────────────────────────────────────────────


PASS1_FILE = "pass1.json"
TRANSCRIPT_JSON_FILE = "transcript.json"   # structured copy used when resuming
NOTES_FILE = "notes.md"


def write_transcript_only(session_dir: Path, transcript: list[TranscriptLine]) -> None:
    """
    Write transcript.md (markdown for humans) + transcript.json (structured,
    for resume) at the end of Pass 1 — so the user always has raw output even
    if they abandon labeling.
    """
    session_dir.mkdir(parents=True, exist_ok=True)
    _write_transcript(session_dir / "transcript.md", transcript)
    _atomic_write(
        session_dir / TRANSCRIPT_JSON_FILE,
        json.dumps([
            {
                "start": ln.start,
                "end": ln.end,
                "speaker_id": ln.speaker_id,
                "speaker_label": ln.speaker_label,
                "text": ln.text,
            }
            for ln in transcript
        ], indent=2),
    )


def read_transcript_json(session_dir: Path) -> list[TranscriptLine]:
    path = session_dir / TRANSCRIPT_JSON_FILE
    if not path.exists():
        return []
    try:
        data = json.loads(path.read_text())
    except Exception:
        return []
    return [
        TranscriptLine(
            start=d.get("start", 0.0),
            end=d.get("end", 0.0),
            speaker_id=d.get("speaker_id", "SPEAKER_00"),
            speaker_label=d.get("speaker_label", "Speaker 1"),
            text=d.get("text", ""),
        )
        for d in data
    ]


def write_pass1_json(session_dir: Path, pass1: Pass1Result) -> None:
    session_dir.mkdir(parents=True, exist_ok=True)
    _atomic_write(session_dir / PASS1_FILE, pass1.model_dump_json(indent=2))


def read_pass1_json(session_dir: Path) -> Pass1Result | None:
    path = session_dir / PASS1_FILE
    if not path.exists():
        return None
    try:
        return Pass1Result.model_validate_json(path.read_text())
    except Exception as exc:
        logger.warning("Failed to read %s: %s", path, exc)
        return None


def is_resumable(session_dir: Path) -> bool:
    """A session is resumable if Pass 1 finished but Pass 2 never wrote notes.md."""
    return (
        session_dir.is_dir()
        and (session_dir / PASS1_FILE).exists()
        and (session_dir / TRANSCRIPT_JSON_FILE).exists()
        and not (session_dir / NOTES_FILE).exists()
    )


def list_resumable_sessions(output_dir: Path) -> list[Path]:
    if not output_dir.exists():
        return []
    return [p for p in sorted(output_dir.iterdir()) if p.is_dir() and is_resumable(p)]


# ── Pass 2 finalizer — produces notes.md + Obsidian export ───────────────────


def save_session(
    session_dir: Path,
    transcript: list[TranscriptLine],
    notes: SessionNotes,
    auto_delete_audio: bool = True,
    tmp_dir: Path | None = None,
) -> Path:
    """
    Write transcript.md and notes.md to session_dir (re-writes transcript.md
    in case labels changed between Pass 1 and Pass 2). Optionally delete all
    WAV files from tmp_dir. Also exports to Obsidian vault if configured.
    Returns session_dir.
    """
    _write_transcript(session_dir / "transcript.md", transcript)
    _write_notes(session_dir / NOTES_FILE, notes)

    if auto_delete_audio and tmp_dir and tmp_dir.exists():
        for wav in tmp_dir.glob("*.wav"):
            wav.unlink(missing_ok=True)

    # Obsidian auto-export
    try:
        _export_to_obsidian(session_dir.name, transcript, notes)
    except Exception as exc:
        logger.warning("Obsidian export failed: %s", exc)

    return session_dir


def _export_to_obsidian(
    session_name: str,
    transcript: list[TranscriptLine],
    notes: SessionNotes,
) -> None:
    """
    Export session to Obsidian vault as a folder containing:
      - Session Notes.md   (summary, plot points, open questions + backlinks)
      - Transcript.md
      - NPCs/<name>.md     (one file per NPC)
      - Locations/<name>.md (one file per location)
    """
    config_path = Path(__file__).parent.parent.parent / "obsidian_config.json"
    if not config_path.exists():
        return
    try:
        config = json.loads(config_path.read_text())
    except Exception:
        return

    vault_path = config.get("vault_path", "")
    if not vault_path or not config.get("auto_export", True):
        return

    vault = Path(vault_path)
    if not vault.exists():
        logger.warning("Obsidian vault not found: %s", vault_path)
        return

    subfolder = config.get("subfolder", "D&D Sessions").strip()
    base_dir = vault / subfolder if subfolder else vault
    safe_name = session_name.replace("/", "-").replace("\\", "-")
    session_dir = base_dir / safe_name
    session_dir.mkdir(parents=True, exist_ok=True)

    date_str = datetime.now().strftime("%Y-%m-%d")
    npc_names = [npc.name for npc in notes.npcs] if notes.npcs else []
    location_names = [loc.name for loc in notes.locations] if notes.locations else []

    # ── Session Notes.md ──────────────────────────────────────────────────
    npc_links = ", ".join(f"[[{n}]]" for n in npc_names) if npc_names else "None yet"
    loc_links = ", ".join(f"[[{n}]]" for n in location_names) if location_names else "None yet"

    notes_parts = [
        f"---",
        f"type: session-notes",
        f"date: {date_str}",
        f"session: \"{safe_name}\"",
        f"npcs: {json.dumps(npc_names)}",
        f"locations: {json.dumps(location_names)}",
        f"tags:",
        f"  - dnd",
        f"  - session",
        f"---",
        f"",
        f"# {safe_name}",
        f"",
    ]

    if notes.summary:
        notes_parts.append(f"## Summary\n\n{notes.summary}\n")

    # Link to NPCs and Locations
    notes_parts.append(f"## NPCs\n\n{npc_links}\n")
    notes_parts.append(f"## Locations\n\n{loc_links}\n")

    if notes.plot_points:
        notes_parts.append("## Plot Points\n")
        for i, pp in enumerate(notes.plot_points, 1):
            involved = ", ".join(f"[[{n}]]" for n in pp.npcs_involved) if pp.npcs_involved else ""
            notes_parts.append(f"{i}. **{pp.summary}**")
            if involved:
                notes_parts.append(f"   *Involving: {involved}*")
            if pp.context:
                notes_parts.append(f"   {pp.context}")
            notes_parts.append("")

    if notes.open_questions:
        notes_parts.append("## Open Questions\n")
        for q in notes.open_questions:
            notes_parts.append(f"- {q}")
        notes_parts.append("")

    notes_parts.append(f"\n---\n*See also: [[Transcript]]*")

    _atomic_write(session_dir / "Session Notes.md", "\n".join(notes_parts))

    # ── Transcript.md ─────────────────────────────────────────────────────
    transcript_parts = [
        f"---",
        f"type: session-transcript",
        f"date: {date_str}",
        f"session: \"{safe_name}\"",
        f"tags:",
        f"  - dnd",
        f"  - transcript",
        f"---",
        f"",
        _write_transcript_str(transcript),
    ]
    _atomic_write(session_dir / "Transcript.md", "\n".join(transcript_parts))

    # ── NPCs/ folder ──────────────────────────────────────────────────────
    if notes.npcs:
        npc_dir = session_dir / "NPCs"
        npc_dir.mkdir(exist_ok=True)
        for npc in notes.npcs:
            npc_parts = [
                f"---",
                f"type: npc",
                f"date: {date_str}",
                f"session: \"{safe_name}\"",
                f"relationship: \"{npc.relationship}\"",
                f"tags:",
                f"  - dnd",
                f"  - npc",
                f"  - {npc.relationship or 'unknown'}",
                f"---",
                f"",
                f"# {npc.name}",
                f"",
            ]
            if npc.relationship:
                npc_parts.append(f"**Relationship:** {npc.relationship}\n")
            if npc.description:
                npc_parts.append(f"## Description\n\n{npc.description}\n")
            if npc.first_seen:
                npc_parts.append(f"**First seen:** {npc.first_seen}")
            if npc.last_seen:
                npc_parts.append(f"**Last seen:** {npc.last_seen}")
            if npc.notes:
                npc_parts.append(f"\n## Notes\n\n{npc.notes}\n")
            npc_parts.append(f"\n---\n*Session: [[Session Notes]]*")

            npc_filename = npc.name.replace("/", "-").replace("\\", "-")
            _atomic_write(npc_dir / f"{npc_filename}.md", "\n".join(npc_parts))

    # ── Locations/ folder ─────────────────────────────────────────────────
    if notes.locations:
        loc_dir = session_dir / "Locations"
        loc_dir.mkdir(exist_ok=True)
        for loc in notes.locations:
            loc_parts = [
                f"---",
                f"type: location",
                f"date: {date_str}",
                f"session: \"{safe_name}\"",
                f"tags:",
                f"  - dnd",
                f"  - location",
                f"---",
                f"",
                f"# {loc.name}",
                f"",
            ]
            if loc.description:
                loc_parts.append(f"{loc.description}\n")
            if loc.significance:
                loc_parts.append(f"## Significance\n\n*{loc.significance}*\n")
            loc_parts.append(f"\n---\n*Session: [[Session Notes]]*")

            loc_filename = loc.name.replace("/", "-").replace("\\", "-")
            _atomic_write(loc_dir / f"{loc_filename}.md", "\n".join(loc_parts))

    logger.info("Obsidian export: %s (%d NPCs, %d locations)",
                session_dir, len(notes.npcs), len(notes.locations))


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
