"""
LLM-powered note organizer — uses Ollama for local inference.

Two-phase approach:
  - During session: No LLM. Just accumulate transcript lines.
  - After session:  Chunked summarization through the full transcript via Ollama.

This avoids memory pressure during recording (Whisper + LLM competing for RAM)
and ensures every minute of the session is processed, not just the tail end.
"""

from __future__ import annotations

import asyncio
import json
import logging
import re

import ollama

from src.notes.models import SessionNotes
from src.notes.prompts import build_messages

logger = logging.getLogger(__name__)

# Lines per chunk sent to the LLM (~300 lines ≈ 6K tokens, fits 8K context)
CHUNK_SIZE = 300

# JSON schema for structured output — Ollama enforces this
_OUTPUT_SCHEMA = SessionNotes.model_json_schema()


class NoteOrganizer:
    """
    Accumulates transcript during a live session, then processes the full
    transcript in chunks via Ollama after the session ends.

    Call start()           → begins the session (no LLM work yet).
    Call update_transcript → feed new lines as they arrive.
    Call stop()            → runs the full chunked summarization.
    Call get_notes()       at any time to retrieve the latest notes.
    """

    def __init__(
        self,
        model: str = "llama3.1:8b",
        update_interval: int = 300,
        player_context: dict | None = None,
    ) -> None:
        self._model          = model
        self._update_interval = update_interval
        self._notes          = SessionNotes()
        self._transcript_lines: list[str] = []
        self._pass_lock      = asyncio.Lock()
        self._player_context = player_context or {}
        self._live_task: asyncio.Task | None = None

    # ── Public API ────────────────────────────────────────────────────────────

    def update_transcript(self, lines: list[str]) -> None:
        """Replace the transcript lines used on the next LLM pass."""
        self._transcript_lines = lines

    def get_notes(self) -> SessionNotes:
        return self._notes

    async def start(self) -> None:
        """Start the session. Kicks off a background loop for periodic live passes."""
        self._live_task = asyncio.create_task(self._live_loop())

    async def stop(self) -> None:
        """Stop the live loop. Does NOT run a final pass — call run_full_pass() separately."""
        if self._live_task:
            self._live_task.cancel()
            try:
                await self._live_task
            except asyncio.CancelledError:
                pass
            self._live_task = None

    async def run_full_pass(self) -> None:
        """Public method to trigger a full chunked pass (e.g. from post-session)."""
        await self._run_chunked_pass()

    # ── Live session loop (lightweight periodic passes) ───────────────────────

    async def _live_loop(self) -> None:
        """
        During a live session, run a pass every update_interval seconds.
        Uses only the most recent lines to keep it fast.
        """
        while True:
            await asyncio.sleep(self._update_interval)
            if self._transcript_lines:
                await self._run_single_pass(self._transcript_lines)

    # ── Single pass (used during live session) ────────────────────────────────

    async def _run_single_pass(self, lines: list[str]) -> None:
        """Run a single LLM pass over the given lines."""
        if not lines:
            return
        if self._pass_lock.locked():
            logger.info("[notes] LLM pass already in progress — skipping")
            return
        async with self._pass_lock:
            loop = asyncio.get_event_loop()
            await loop.run_in_executor(None, self._sync_single_pass, lines)

    def _sync_single_pass(self, lines: list[str]) -> None:
        """Synchronous single LLM pass."""
        # Use only the tail for live passes to stay within context window
        if len(lines) > CHUNK_SIZE:
            lines = lines[-CHUNK_SIZE:]

        transcript = "\n".join(lines)
        existing_json = (
            self._notes.model_dump_json()
            if (self._notes.npcs or self._notes.summary)
            else ""
        )
        messages = build_messages(transcript, existing_json, self._player_context)

        try:
            response = ollama.chat(
                model=self._model,
                messages=messages,
                format=_OUTPUT_SCHEMA,
                options={"num_predict": 8192, "temperature": 0.3},
            )
            raw = response["message"]["content"]
            new_notes = self._parse_response(raw)
            if new_notes:
                self._notes = _merge(self._notes, new_notes)
                logger.info(
                    "[notes] Live pass — %d NPCs, %d locations, summary=%s",
                    len(self._notes.npcs),
                    len(self._notes.locations),
                    "yes" if self._notes.summary else "no",
                )
        except Exception as exc:
            logger.error("[notes] Live LLM pass failed: %s", exc)

    # ── Chunked post-session pass ─────────────────────────────────────────────

    async def _run_chunked_pass(self) -> None:
        """Process the full transcript in chunks, carrying forward context."""
        if not self._transcript_lines:
            return
        async with self._pass_lock:
            loop = asyncio.get_event_loop()
            await loop.run_in_executor(None, self._sync_chunked_pass)

    def _sync_chunked_pass(self) -> None:
        """
        Process the entire transcript in CHUNK_SIZE windows.
        Each chunk gets the accumulated notes from prior chunks as context,
        so the final result reflects the full session.
        """
        lines = self._transcript_lines
        total = len(lines)

        if total == 0:
            return

        # Split into chunks
        chunks = [lines[i:i + CHUNK_SIZE] for i in range(0, total, CHUNK_SIZE)]
        logger.info(
            "[notes] Starting chunked pass: %d lines in %d chunk(s)",
            total, len(chunks),
        )

        accumulated = SessionNotes()

        for i, chunk in enumerate(chunks):
            chunk_num = i + 1
            logger.info("[notes] Processing chunk %d/%d (%d lines)...",
                        chunk_num, len(chunks), len(chunk))

            transcript = "\n".join(chunk)
            existing_json = (
                accumulated.model_dump_json()
                if (accumulated.npcs or accumulated.summary)
                else ""
            )
            messages = build_messages(transcript, existing_json, self._player_context)

            try:
                response = ollama.chat(
                    model=self._model,
                    messages=messages,
                    options={"num_predict": 8192, "temperature": 0.3},
                )
                raw = response["message"]["content"]
                new_notes = self._parse_response(raw)

                if new_notes:
                    accumulated = _merge(accumulated, new_notes)

                    # Log progress
                    filled = []
                    if accumulated.summary: filled.append("summary")
                    if accumulated.npcs: filled.append(f"{len(accumulated.npcs)} NPCs")
                    if accumulated.locations: filled.append(f"{len(accumulated.locations)} locs")
                    if accumulated.plot_points: filled.append(f"{len(accumulated.plot_points)} plots")
                    if accumulated.open_questions: filled.append(f"{len(accumulated.open_questions)} questions")
                    logger.info("[notes] Chunk %d/%d done: %s",
                                chunk_num, len(chunks), ", ".join(filled))
                else:
                    logger.warning("[notes] Chunk %d/%d returned no parseable notes",
                                   chunk_num, len(chunks))

            except Exception as exc:
                logger.error("[notes] Chunk %d/%d failed: %s", chunk_num, len(chunks), exc)
                continue

        self._notes = accumulated
        logger.info(
            "[notes] Chunked pass complete — summary=%s, %d NPCs, %d locations, "
            "%d plot points, %d open questions",
            "yes" if self._notes.summary else "no",
            len(self._notes.npcs),
            len(self._notes.locations),
            len(self._notes.plot_points),
            len(self._notes.open_questions),
        )

    # ── Response parsing ──────────────────────────────────────────────────────

    def _parse_response(self, raw: str) -> SessionNotes | None:
        """Parse LLM response into SessionNotes, coercing simplified formats."""
        json_str = _extract_json(raw)
        if not json_str:
            logger.warning("[notes] Could not extract JSON from LLM response")
            logger.warning("[notes] Raw (first 800 chars): %s", raw[:800])
            return None

        try:
            data = json.loads(json_str)

            # Coerce plain strings → objects for NPCs (e.g. "Gorzav" → {"name": "Gorzav"})
            if data.get("npcs"):
                data["npcs"] = [
                    {"name": item} if isinstance(item, str) else item
                    for item in data["npcs"]
                ]

            # Coerce plain strings → objects for locations
            if data.get("locations"):
                data["locations"] = [
                    {"name": item} if isinstance(item, str) else item
                    for item in data["locations"]
                ]

            # Coerce plain strings → objects for plot points
            if data.get("plot_points"):
                data["plot_points"] = [
                    {"summary": item} if isinstance(item, str) else item
                    for item in data["plot_points"]
                ]

            notes = SessionNotes.model_validate(data)

            # Log which fields were populated
            filled = []
            if notes.summary: filled.append("summary")
            if notes.npcs: filled.append(f"{len(notes.npcs)} NPCs")
            if notes.locations: filled.append(f"{len(notes.locations)} locations")
            if notes.plot_points: filled.append(f"{len(notes.plot_points)} plot points")
            if notes.open_questions: filled.append(f"{len(notes.open_questions)} questions")
            logger.info("[notes] LLM returned: %s", ", ".join(filled) or "(empty)")

            return notes
        except Exception as exc:
            logger.error("[notes] Failed to parse LLM JSON: %s", exc)
            logger.debug("[notes] JSON string: %s", json_str[:500])
            return None


# ── Helpers ───────────────────────────────────────────────────────────────────

def _extract_json(text: str) -> str | None:
    """
    Extract a JSON object from LLM output.
    Handles Qwen3 <think> tags, markdown code fences, bare JSON, and truncated output.
    """
    # Strip Qwen3 thinking tags if present
    text = re.sub(r"<think>.*?</think>", "", text, flags=re.DOTALL).strip()

    # Strip markdown fences if present
    fenced = re.search(r"```(?:json)?\s*(\{.*?\})\s*```", text, re.DOTALL)
    if fenced:
        candidate = fenced.group(1)
        try:
            json.loads(candidate)
            return candidate
        except json.JSONDecodeError:
            pass

    # Find the outermost { ... } block
    start = text.find("{")
    if start == -1:
        return None
    depth = 0
    for i, ch in enumerate(text[start:], start):
        if ch == "{":
            depth += 1
        elif ch == "}":
            depth -= 1
            if depth == 0:
                candidate = text[start : i + 1]
                try:
                    json.loads(candidate)
                    return candidate
                except json.JSONDecodeError:
                    return None

    # JSON was truncated (depth > 0) — try to repair by closing open structures
    truncated = text[start:]
    repaired = _repair_truncated_json(truncated)
    if repaired:
        return repaired

    return None


def _repair_truncated_json(text: str) -> str | None:
    """
    Attempt to repair truncated JSON by removing the incomplete tail
    and closing open brackets/braces.
    """
    # Find the last complete value (ends with ", or ], or }, or a literal)
    # Trim back to the last comma, closing bracket, or closing brace
    for trim_char in [",", "]", "}"]:
        idx = text.rfind(trim_char)
        if idx > 0:
            candidate = text[:idx + 1].rstrip(",")
            # Count open/close braces and brackets
            open_braces = candidate.count("{") - candidate.count("}")
            open_brackets = candidate.count("[") - candidate.count("]")
            # Close them
            candidate += "]" * open_brackets + "}" * open_braces
            try:
                json.loads(candidate)
                logger.warning("[notes] Repaired truncated JSON (trimmed at char %d)", idx)
                return candidate
            except json.JSONDecodeError:
                continue
    return None


def _merge(existing: SessionNotes, fresh: SessionNotes) -> SessionNotes:
    """
    Merge fresh LLM output into existing notes.
    Summary always comes from fresh (it has the latest context).
    NPCs and locations are union-merged, preferring fresh data on conflicts.
    Plot points and questions accumulate.
    """
    return SessionNotes(
        summary=fresh.summary or existing.summary,
        npcs=_merge_by_name(existing.npcs, fresh.npcs, key="name"),
        locations=_merge_by_name(existing.locations, fresh.locations, key="name"),
        plot_points=_merge_plot_points(existing.plot_points, fresh.plot_points),
        open_questions=_merge_questions(existing.open_questions, fresh.open_questions),
    )


def _merge_by_name(existing: list, fresh: list, key: str) -> list:
    index = {getattr(item, key).lower(): item for item in existing}
    for item in fresh:
        index[getattr(item, key).lower()] = item  # fresh always wins
    return list(index.values())


def _merge_plot_points(existing: list, fresh: list) -> list:
    """Merge plot points, avoiding near-duplicates."""
    seen = {pp.summary.lower().strip() for pp in existing}
    merged = list(existing)
    for pp in fresh:
        key = pp.summary.lower().strip()
        if key not in seen:
            merged.append(pp)
            seen.add(key)
    return merged


def _merge_questions(existing: list[str], fresh: list[str]) -> list[str]:
    """Merge open questions, avoiding duplicates."""
    seen = {q.lower().strip() for q in existing}
    merged = list(existing)
    for q in fresh:
        key = q.lower().strip()
        if key not in seen:
            merged.append(q)
            seen.add(key)
    return merged
