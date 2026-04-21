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


# Primary notes model, with fallbacks when 2.5-flash is overloaded (503) or
# the per-minute free-tier limit is hit (429). `flash-lite` is lighter and
# has a separate rate-limit bucket on the free tier; it's our safety net.
# Order matters — we stop at the first model that succeeds.
_MODEL_FALLBACKS = [
    "gemini-2.5-flash",
    "gemini-2.5-flash-lite",
    "gemini-2.5-pro",
]

# Retry policy for transient errors (503 UNAVAILABLE, 429 RESOURCE_EXHAUSTED).
# Exponential-ish backoff, capped so a truly-down provider doesn't hold the
# session hostage. Total wall time ~14s per model before falling through.
_RETRY_DELAYS_SECONDS = [1, 3, 6]

# Back-compat alias for any tests that reference it
_MODEL = _MODEL_FALLBACKS[0]


class NoteOrganizerError(RuntimeError):
    pass


def _is_transient(exc: Exception) -> bool:
    """503 UNAVAILABLE or 429 RESOURCE_EXHAUSTED — worth retrying or falling through."""
    msg = str(exc)
    return "503" in msg or "UNAVAILABLE" in msg or "429" in msg or "RESOURCE_EXHAUSTED" in msg


def _generate_with_fallback(client, *, contents, config, call_label: str):
    """
    Call Gemini with automatic retry-then-model-fallback.

    Tries each model in _MODEL_FALLBACKS in order; for each, retries with
    exponential backoff on transient 503/429. Falls through to the next
    model on persistent transient errors. Returns the response or None.
    Non-transient errors abort immediately (caller logs and handles None).
    """
    import time
    last_exc: Optional[Exception] = None
    for model in _MODEL_FALLBACKS:
        for attempt, delay in enumerate([0, *_RETRY_DELAYS_SECONDS]):
            if delay:
                time.sleep(delay)
            try:
                response = client.models.generate_content(
                    model=model, contents=contents, config=config,
                )
                if attempt > 0 or model != _MODEL_FALLBACKS[0]:
                    logger.info("[notes] %s succeeded on %s (attempt %d)", call_label, model, attempt + 1)
                return response
            except Exception as exc:
                last_exc = exc
                if not _is_transient(exc):
                    logger.error("[notes] %s non-transient failure on %s: %s", call_label, model, exc)
                    return None
                logger.warning(
                    "[notes] %s transient failure on %s (attempt %d): %s",
                    call_label, model, attempt + 1, str(exc)[:160],
                )
        logger.warning("[notes] %s exhausted retries on %s — falling through to next model", call_label, model)
    logger.error("[notes] %s failed on all models. Last error: %s", call_label, last_exc)
    return None


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

        response = _generate_with_fallback(
            client,
            contents=user,
            config=genai_types.GenerateContentConfig(
                system_instruction=system,
                response_mime_type="application/json",
                response_schema=SessionNotes,
                temperature=0.3,
            ),
            call_label=f"notes {mode}",
        )
        if response is None:
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

        response = _generate_with_fallback(
            client,
            contents=user,
            config=genai_types.GenerateContentConfig(
                system_instruction=system,
                response_mime_type="application/json",
                response_schema=Pass1Result,
                temperature=0.2,
            ),
            call_label="pass1",
        )
        if response is None:
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
