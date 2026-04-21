"""
Gemini-powered note organizer — two-pass post-session flow.

  - refresh_preview()  — mid-session fast pass over a partial transcript.
                         Used every 15 minutes during the live session so
                         the notes panel updates as play unfolds.

  - run_pass1(lines)   — post-session Pass 1. Produces per-speaker summaries
                         + per-utterance in_character/other tags. Does NOT
                         produce final notes.

  - run_pass2(...)     — post-session Pass 2. Takes the user-assigned speaker
                         labels and the in_character filter, produces final
                         SessionNotes.

The session manager owns all timing. The organizer is stateless about when
things happen.
"""

from __future__ import annotations

import asyncio
import logging
from typing import Optional

from google import genai
from google.genai import types as genai_types

from src import cloud_config
from src.campaign.models import Campaign
from src.notes.models import Pass1Result, SessionNotes, UtteranceTag
from src.notes.prompts import (
    build_pass1_system_prompt,
    build_pass1_user_prompt,
    build_system_prompt,
    build_user_prompt,
)

logger = logging.getLogger(__name__)


_MODEL = "gemini-2.5-flash"


class NoteOrganizerError(RuntimeError):
    pass


class NoteOrganizer:
    def __init__(self, campaign: Optional[Campaign] = None) -> None:
        self._campaign = campaign
        self._notes = SessionNotes()
        self._transcript_text: str = ""
        self._pass_lock = asyncio.Lock()

    # ── Public API ────────────────────────────────────────────────────────────

    def update_transcript(self, transcript_text: str) -> None:
        self._transcript_text = transcript_text

    def get_notes(self) -> SessionNotes:
        return self._notes

    def set_notes(self, notes: SessionNotes) -> None:
        """Used by the session manager to restore state from disk on resume."""
        self._notes = notes

    async def refresh_preview(self) -> bool:
        """Run one mid-session preview pass. Returns True if notes were updated."""
        if not self._transcript_text.strip():
            return False
        if self._pass_lock.locked():
            logger.info("[notes] Preview refresh skipped — prior pass still running")
            return False
        async with self._pass_lock:
            existing_json = (
                self._notes.model_dump_json()
                if (self._notes.summary or self._notes.npcs)
                else ""
            )
            loop = asyncio.get_event_loop()
            notes = await loop.run_in_executor(
                None, self._call_notes_gemini, self._transcript_text, existing_json, "preview",
            )
            if notes is None:
                return False
            self._notes = notes
            return True

    async def run_pass1(self, transcript_lines: list[str]) -> Optional[Pass1Result]:
        """
        Post-session Pass 1. transcript_lines are pre-rendered strings, one
        per utterance, with their 0-based index as the square-bracketed prefix.
        Returns Pass1Result or None on failure.
        """
        if not transcript_lines:
            return None
        async with self._pass_lock:
            loop = asyncio.get_event_loop()
            return await loop.run_in_executor(
                None, self._call_pass1_gemini, transcript_lines,
            )

    async def run_pass2(
        self,
        filtered_transcript_text: str,
    ) -> bool:
        """
        Post-session Pass 2. Called with the already-labeled-and-filtered
        transcript text (caller handles the filter + label application).
        Overwrites notes. Returns True if notes were produced.
        """
        if not filtered_transcript_text.strip():
            logger.info("[notes] Pass 2 skipped — empty filtered transcript")
            return False
        async with self._pass_lock:
            loop = asyncio.get_event_loop()
            notes = await loop.run_in_executor(
                None, self._call_notes_gemini, filtered_transcript_text, "", "full",
            )
            if notes is None:
                return False
            self._notes = notes
            return True

    # ── Gemini call: notes extraction (preview + pass 2) ─────────────────────

    def _call_notes_gemini(self, transcript: str, existing_json: str, mode: str) -> Optional[SessionNotes]:
        key = cloud_config.get_gemini_key()
        if not key:
            raise NoteOrganizerError("No Gemini API key configured. Add one in Settings → API Keys.")

        client = genai.Client(api_key=key)
        system = build_system_prompt(self._campaign)
        user = build_user_prompt(transcript, existing_json, mode=mode)

        try:
            response = client.models.generate_content(
                model=_MODEL,
                contents=user,
                config=genai_types.GenerateContentConfig(
                    system_instruction=system,
                    response_mime_type="application/json",
                    response_schema=SessionNotes,
                    temperature=0.3,
                ),
            )
        except Exception as exc:
            logger.error("[notes] Gemini notes call failed: %s", exc)
            return None

        raw = (getattr(response, "text", "") or "").strip()
        if not raw:
            logger.warning("[notes] Gemini returned empty text")
            return None
        try:
            notes = SessionNotes.model_validate_json(raw)
        except Exception as exc:
            logger.error("[notes] Failed to parse notes JSON: %s; raw[:400]=%s", exc, raw[:400])
            return None

        filled = []
        if notes.summary: filled.append("summary")
        if notes.npcs: filled.append(f"{len(notes.npcs)} NPCs")
        if notes.locations: filled.append(f"{len(notes.locations)} locations")
        if notes.plot_points: filled.append(f"{len(notes.plot_points)} plots")
        if notes.open_questions: filled.append(f"{len(notes.open_questions)} questions")
        logger.info("[notes] Gemini %s pass → %s", mode, ", ".join(filled) or "(empty)")
        return notes

    # ── Gemini call: Pass 1 (speaker summaries + classification) ─────────────

    def _call_pass1_gemini(self, transcript_lines: list[str]) -> Optional[Pass1Result]:
        key = cloud_config.get_gemini_key()
        if not key:
            raise NoteOrganizerError("No Gemini API key configured. Add one in Settings → API Keys.")

        client = genai.Client(api_key=key)
        system = build_pass1_system_prompt(self._campaign)
        user = build_pass1_user_prompt(transcript_lines)

        try:
            response = client.models.generate_content(
                model=_MODEL,
                contents=user,
                config=genai_types.GenerateContentConfig(
                    system_instruction=system,
                    response_mime_type="application/json",
                    response_schema=Pass1Result,
                    temperature=0.2,
                ),
            )
        except Exception as exc:
            logger.error("[notes] Gemini pass1 call failed: %s", exc)
            return None

        raw = (getattr(response, "text", "") or "").strip()
        if not raw:
            logger.warning("[notes] Gemini pass1 returned empty text")
            return None
        try:
            result = Pass1Result.model_validate_json(raw)
        except Exception as exc:
            logger.error("[notes] Failed to parse pass1 JSON: %s; raw[:400]=%s", exc, raw[:400])
            return None

        # Normalize tags: pad any missing indices with "in_character" (safety),
        # and clamp out-of-range indices. This is belt-and-suspenders — the
        # prompt already insists on one tag per index, but LLMs miscount.
        tag_by_idx = {t.index: t for t in result.tags}
        normalized: list[UtteranceTag] = []
        for i in range(len(transcript_lines)):
            existing = tag_by_idx.get(i)
            if existing and existing.tag in ("in_character", "other"):
                normalized.append(UtteranceTag(index=i, tag=existing.tag))
            else:
                normalized.append(UtteranceTag(index=i, tag="in_character"))
        result.tags = normalized

        in_char = sum(1 for t in result.tags if t.tag == "in_character")
        logger.info(
            "[notes] Pass1 → %d speakers, %d in_character / %d other (of %d lines)",
            len(result.speakers), in_char, len(result.tags) - in_char, len(transcript_lines),
        )
        return result
