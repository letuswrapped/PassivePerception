"""
LLM-powered note organizer — runs on Apple Silicon Neural Engine via mlx-lm.

No daemon, no HTTP, no external process. The model is lazy-loaded on the first
note pass and unloaded (with cache cleared) after the session ends.
"""

from __future__ import annotations

import asyncio
import gc
import json
import logging
import re

from src.notes.models import SessionNotes
from src.notes.prompts import build_messages

logger = logging.getLogger(__name__)


class NoteOrganizer:
    """
    Runs periodic LLM passes over the accumulated transcript and maintains
    a merged SessionNotes object.

    Call start()    → begins the background update loop.
    Call stop()     → halts the loop, runs one final pass, unloads model.
    Call get_notes() at any time to retrieve the latest notes.
    """

    def __init__(
        self,
        model: str = "mlx-community/Llama-3.2-3B-Instruct-4bit",
        update_interval: int = 300,
        player_context: dict | None = None,
    ) -> None:
        self._model_repo     = model
        self._update_interval = update_interval
        self._notes          = SessionNotes()
        self._transcript_lines: list[str] = []
        self._task: asyncio.Task | None   = None
        self._mlx_model      = None
        self._mlx_tokenizer  = None
        self._pass_lock      = asyncio.Lock()
        self._player_context = player_context or {}

    # ── Public API ────────────────────────────────────────────────────────────

    def update_transcript(self, lines: list[str]) -> None:
        """Replace the transcript lines used on the next LLM pass."""
        self._transcript_lines = lines

    def get_notes(self) -> SessionNotes:
        return self._notes

    async def start(self) -> None:
        self._task = asyncio.create_task(self._loop())

    async def stop(self) -> None:
        if self._task:
            self._task.cancel()
            try:
                await self._task
            except asyncio.CancelledError:
                pass
        # Final pass before session is saved
        await self._run_pass()
        self._unload()

    # ── Model lifecycle ───────────────────────────────────────────────────────

    def _ensure_loaded(self) -> None:
        if self._mlx_model is None:
            from mlx_lm import load
            logger.info("[notes] Loading LLM: %s", self._model_repo)
            self._mlx_model, self._mlx_tokenizer = load(self._model_repo)
            logger.info("[notes] LLM ready")

    def _unload(self) -> None:
        if self._mlx_model is not None:
            del self._mlx_model
            del self._mlx_tokenizer
            self._mlx_model     = None
            self._mlx_tokenizer = None
            gc.collect()
            try:
                import mlx.core as mx
                mx.metal.clear_cache()
                logger.info("[notes] LLM unloaded, Metal cache cleared")
            except Exception:
                pass

    # ── Update loop ───────────────────────────────────────────────────────────

    async def _loop(self) -> None:
        while True:
            await asyncio.sleep(self._update_interval)
            await self._run_pass()

    async def _run_pass(self) -> None:
        if not self._transcript_lines:
            return
        if self._pass_lock.locked():
            logger.info("[notes] LLM pass already in progress — skipping")
            return
        async with self._pass_lock:
            loop = asyncio.get_event_loop()
            await loop.run_in_executor(None, self._sync_pass)

    def _sync_pass(self) -> None:
        from mlx_lm import generate

        self._ensure_loaded()

        # Truncate transcript to fit context window (~8k tokens max for transcript)
        # Keep the most recent lines; the existing notes carry forward older context
        MAX_TRANSCRIPT_LINES = 400  # ~8k tokens at ~20 tokens/line
        lines = self._transcript_lines
        if len(lines) > MAX_TRANSCRIPT_LINES:
            lines = lines[-MAX_TRANSCRIPT_LINES:]
            logger.info("[notes] Transcript truncated: using last %d of %d lines",
                        MAX_TRANSCRIPT_LINES, len(self._transcript_lines))

        transcript    = "\n".join(lines)
        existing_json = (
            self._notes.model_dump_json()
            if (self._notes.npcs or self._notes.summary)
            else ""
        )
        messages = build_messages(transcript, existing_json, self._player_context)

        # Build prompt using the model's chat template
        prompt = self._mlx_tokenizer.apply_chat_template(
            messages,
            tokenize=False,
            add_generation_prompt=True,
        )

        try:
            raw = generate(
                self._mlx_model,
                self._mlx_tokenizer,
                prompt=prompt,
                max_tokens=2048,
                verbose=False,
            )

            json_str = _extract_json(raw)
            if not json_str:
                logger.warning("[notes] Could not extract JSON from LLM response")
                logger.debug("[notes] Raw response: %s", raw[:500])
                return

            new_notes = SessionNotes.model_validate_json(json_str)
            self._notes = _merge(self._notes, new_notes)
            logger.info(
                "[notes] Updated — %d NPCs, %d locations, %d plot points",
                len(self._notes.npcs),
                len(self._notes.locations),
                len(self._notes.plot_points),
            )

        except Exception as exc:
            logger.error("[notes] LLM pass failed: %s", exc)


# ── Helpers ───────────────────────────────────────────────────────────────────

def _extract_json(text: str) -> str | None:
    """
    Extract a JSON object from LLM output.
    Handles markdown code fences (```json ... ```) and bare JSON objects.
    """
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
    return None


def _merge(existing: SessionNotes, fresh: SessionNotes) -> SessionNotes:
    """
    Merge fresh LLM output into existing notes.
    Summary and plot points always come from fresh (it saw the full transcript).
    NPCs and locations are union-merged, preferring fresh data on conflicts.
    """
    return SessionNotes(
        summary=fresh.summary or existing.summary,
        npcs=_merge_by_name(existing.npcs, fresh.npcs, key="name"),
        locations=_merge_by_name(existing.locations, fresh.locations, key="name"),
        plot_points=fresh.plot_points or existing.plot_points,
        open_questions=fresh.open_questions or existing.open_questions,
    )


def _merge_by_name(existing: list, fresh: list, key: str) -> list:
    index = {getattr(item, key).lower(): item for item in existing}
    for item in fresh:
        index[getattr(item, key).lower()] = item  # fresh always wins
    return list(index.values())
