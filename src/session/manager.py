"""
Session lifecycle — two-pass cloud backend.

Live session:
  Audio → AudioBuffer → WAV chunks on disk.
  Every preview_interval seconds: concatenate new chunks → Deepgram preview
  (no diarization) → append to running transcript text → Gemini preview pass
  → broadcast notes.

Stop:
  1. Flush audio.
  2. PROCESSING_PASS1 — concat all chunks → Deepgram full (diarization +
     keyterm boost) → canonical TranscriptLine[] → write transcript.md +
     transcript.json. Then Gemini Pass 1 → Pass1Result → pass1.json.
     State advances to AWAITING_LABELS.
  3. User labels speakers via the UI, then POSTs /session/finalize.
  4. PROCESSING_PASS2 — apply labels, filter to in_character utterances
     (with 70% safety fallback), Gemini notes pass, save notes.md + Obsidian
     export, merge into campaign, cleanup. State → IDLE.

Resume:
  If the app quit at AWAITING_LABELS, a session_dir with pass1.json +
  transcript.json + no notes.md can be rehydrated via resume_from_pass1().
"""

from __future__ import annotations

import asyncio
import logging
from datetime import datetime
from pathlib import Path
from typing import Optional

from src.audio.buffer import AudioBuffer
from src.audio.capture import AudioCapture
from src.campaign.models import Campaign
from src.notes.models import Pass1Result, SessionNotes
from src.notes.organizer import NoteOrganizer
from src.transcription import TranscriptLine, default_speaker_label
from src.transcription.audio_utils import concatenate_audio_chunks
from src.transcription.deepgram_client import (
    DeepgramError,
    transcribe_full,
    transcribe_preview,
)

logger = logging.getLogger(__name__)


class SessionState:
    IDLE                = "idle"
    RUNNING             = "running"
    STOPPING            = "stopping"
    PROCESSING_PASS1    = "processing_pass1"
    AWAITING_LABELS     = "awaiting_labels"
    PROCESSING_PASS2    = "processing_pass2"


# If Pass 1 marks more than this fraction of utterances as "other", we assume
# the classifier went haywire and disable the filter for Pass 2 (still pass
# the full transcript; treat tags as display-only in the UI).
_CLASSIFIER_SAFETY_THRESHOLD = 0.70


class SessionManager:
    def __init__(
        self,
        config: dict,
        campaign: Optional[Campaign] = None,
    ) -> None:
        self._cfg = config
        self._campaign = campaign
        self._state = SessionState.IDLE
        self._started_at: Optional[datetime] = None
        self._session_dir: Optional[Path] = None
        self._progress_message: str = ""

        self._capture = AudioCapture(
            device_name=config["audio"]["device"],
            target_rate=config["audio"]["sample_rate"],
        )
        self._buffer = AudioBuffer(
            sample_rate=config["audio"]["sample_rate"],
            chunk_duration=config["audio"]["chunk_duration"],
            tmp_dir=Path(config["output"]["tmp_directory"]),
        )
        self._organizer = NoteOrganizer(campaign=campaign)

        # Session state
        self._chunk_queue: asyncio.Queue[Path] = asyncio.Queue()
        self._all_chunk_paths: list[Path] = []
        self._preview_transcribed_paths: set[Path] = set()
        self._preview_utterances_text: list[str] = []
        self._canonical_transcript: list[TranscriptLine] = []
        self._pass1_result: Optional[Pass1Result] = None
        self._speaker_labels: dict[str, str] = {}
        self._diar_version: int = 0

        # Tasks
        self._chunk_drain_task: Optional[asyncio.Task] = None
        self._preview_task: Optional[asyncio.Task] = None
        self._autosave_task: Optional[asyncio.Task] = None
        self._health_task: Optional[asyncio.Task] = None

        # Callbacks
        self._on_transcript_line: list = []
        self._on_notes_update: list = []
        self._on_pass1_ready: list = []

        # Tunables
        self._preview_interval = int(config.get("notes", {}).get("update_interval", 900))

    # ── Public API ────────────────────────────────────────────────────────────

    def set_mic_device(self, device_name: Optional[str]) -> None:
        self._capture.set_mic_device(device_name)
        logger.info("Mic device set: %s", device_name or "(none)")

    async def start(self, session_name: Optional[str] = None) -> None:
        if self._state != SessionState.IDLE:
            return
        self._state = SessionState.RUNNING
        self._started_at = datetime.now()
        self._progress_message = ""
        self._canonical_transcript = []
        self._all_chunk_paths = []
        self._preview_transcribed_paths = set()
        self._preview_utterances_text = []
        self._pass1_result = None
        self._diar_version = 0

        name = session_name or self._started_at.strftime("%Y-%m-%d_session")
        self._session_dir = Path(self._cfg["output"]["directory"]) / name
        self._session_dir.mkdir(parents=True, exist_ok=True)

        loop = asyncio.get_event_loop()
        self._buffer.set_output_queue(self._chunk_queue, loop)
        self._buffer.start()
        self._capture.add_callback(self._buffer.feed)
        self._capture.start()

        self._chunk_drain_task = asyncio.create_task(self._drain_chunks_loop())
        self._preview_task = asyncio.create_task(self._preview_loop())
        self._autosave_task = asyncio.create_task(self._auto_save_loop())
        self._health_task = asyncio.create_task(self._health_check_loop())
        logger.info("Session started: %s", name)

    async def stop(self) -> None:
        """Stop capture and hand off to Pass 1 processing in the background."""
        if self._state != SessionState.RUNNING:
            raise RuntimeError("No active session.")
        self._state = SessionState.STOPPING

        self._capture.stop()
        self._buffer.stop()

        for task in (self._health_task, self._autosave_task, self._preview_task, self._chunk_drain_task):
            if task:
                task.cancel()
                try:
                    await task
                except asyncio.CancelledError:
                    pass
        self._health_task = None
        self._autosave_task = None
        self._preview_task = None
        self._chunk_drain_task = None

        # Drain queued chunks
        while not self._chunk_queue.empty():
            try:
                path = self._chunk_queue.get_nowait()
                if path not in self._all_chunk_paths:
                    self._all_chunk_paths.append(path)
            except asyncio.QueueEmpty:
                break

        self._state = SessionState.PROCESSING_PASS1
        asyncio.create_task(self._post_session_pass1())

    def rename_speaker(self, speaker_id: str, label: str) -> None:
        self._speaker_labels[speaker_id] = label
        for line in list(self._canonical_transcript):
            if line.speaker_id == speaker_id:
                line.speaker_label = label

    def apply_labels(self, labels: dict[str, str]) -> None:
        for speaker_id, label in labels.items():
            if not speaker_id or not label:
                continue
            self.rename_speaker(speaker_id, label.strip())

    async def finalize(self, labels: Optional[dict[str, str]] = None, skip: bool = False) -> None:
        """Kick off Pass 2 with the user-supplied labels (or default labels if skip=True)."""
        if self._state != SessionState.AWAITING_LABELS:
            raise RuntimeError(f"Cannot finalize — current state is {self._state}")
        if labels:
            self.apply_labels(labels)
        # If skipping, we leave whatever labels exist (default "Speaker N")
        self._state = SessionState.PROCESSING_PASS2
        asyncio.create_task(self._post_session_pass2())

    # ── Accessors ────────────────────────────────────────────────────────────

    def get_transcript(self) -> list[TranscriptLine]:
        return list(self._canonical_transcript)

    def get_notes(self) -> SessionNotes:
        return self._organizer.get_notes()

    def get_pass1_result(self) -> Optional[Pass1Result]:
        return self._pass1_result

    @property
    def session_id(self) -> Optional[str]:
        return self._session_dir.name if self._session_dir else None

    @property
    def state(self) -> str:
        return self._state

    @property
    def progress_message(self) -> str:
        return self._progress_message

    @property
    def diar_version(self) -> int:
        return self._diar_version

    @property
    def elapsed_seconds(self) -> float:
        if self._started_at is None:
            return 0.0
        return (datetime.now() - self._started_at).total_seconds()

    def on_transcript_line(self, cb) -> None:
        self._on_transcript_line.append(cb)

    def on_notes_update(self, cb) -> None:
        self._on_notes_update.append(cb)

    def on_pass1_ready(self, cb) -> None:
        self._on_pass1_ready.append(cb)

    # ── Resume from disk ─────────────────────────────────────────────────────

    @classmethod
    def resume_from_pass1(
        cls,
        config: dict,
        campaign: Optional[Campaign],
        session_dir: Path,
    ) -> "SessionManager":
        """
        Rehydrate a session that completed Pass 1 but never finalized.
        Returns a SessionManager in AWAITING_LABELS state ready to accept
        labels and run Pass 2.
        """
        from src.session.storage import read_pass1_json, read_transcript_json

        mgr = cls(config, campaign=campaign)
        mgr._session_dir = session_dir
        mgr._canonical_transcript = read_transcript_json(session_dir)
        mgr._pass1_result = read_pass1_json(session_dir)
        if not mgr._canonical_transcript or not mgr._pass1_result:
            raise RuntimeError(f"Cannot resume {session_dir} — missing transcript or pass1 artifacts")
        mgr._state = SessionState.AWAITING_LABELS
        mgr._progress_message = "Waiting for speaker labels (resumed)"
        mgr._diar_version = 1
        # Pre-populate speaker_labels from whatever labels are currently on the transcript
        for line in mgr._canonical_transcript:
            if line.speaker_id and line.speaker_label:
                mgr._speaker_labels[line.speaker_id] = line.speaker_label
        logger.info("Resumed session %s at AWAITING_LABELS", session_dir.name)
        return mgr

    # ── Live chunk drain ─────────────────────────────────────────────────────

    async def _drain_chunks_loop(self) -> None:
        while self._state == SessionState.RUNNING:
            try:
                path = await asyncio.wait_for(self._chunk_queue.get(), timeout=1.0)
                if path not in self._all_chunk_paths:
                    self._all_chunk_paths.append(path)
            except asyncio.TimeoutError:
                continue
            except asyncio.CancelledError:
                break

    # ── Preview loop (15-min Deepgram + Gemini during the session) ───────────

    async def _preview_loop(self) -> None:
        while self._state == SessionState.RUNNING:
            try:
                await asyncio.sleep(self._preview_interval)
            except asyncio.CancelledError:
                return
            if self._state != SessionState.RUNNING:
                return
            try:
                await self._run_preview_cycle()
            except Exception as exc:
                logger.error("Preview cycle failed: %s", exc)

    async def _run_preview_cycle(self) -> None:
        new_chunks = [p for p in self._all_chunk_paths if p not in self._preview_transcribed_paths]
        if not new_chunks:
            return

        logger.info("Preview cycle — transcribing %d new chunk(s)", len(new_chunks))
        self._progress_message = "Updating notes…"

        tmp_dir = Path(self._cfg["output"]["tmp_directory"])
        preview_wav = tmp_dir / f"preview_{int(datetime.now().timestamp())}.wav"

        loop = asyncio.get_event_loop()
        try:
            await loop.run_in_executor(
                None,
                lambda: concatenate_audio_chunks(new_chunks, preview_wav, self._cfg["audio"]["sample_rate"]),
            )
        except Exception as exc:
            logger.error("Preview concatenation failed: %s", exc)
            return

        keyterms = self._campaign.keyterms() if self._campaign else []
        try:
            result = await loop.run_in_executor(
                None, lambda: transcribe_preview(preview_wav, keyterms),
            )
        except DeepgramError as exc:
            logger.warning("Preview transcription skipped: %s", exc)
            preview_wav.unlink(missing_ok=True)
            return
        except Exception as exc:
            logger.error("Preview transcription failed: %s", exc)
            preview_wav.unlink(missing_ok=True)
            return
        finally:
            preview_wav.unlink(missing_ok=True)

        for utt in result.utterances:
            if utt.text:
                self._preview_utterances_text.append(utt.text)

        for p in new_chunks:
            self._preview_transcribed_paths.add(p)

        full_text = "\n".join(self._preview_utterances_text)
        self._organizer.update_transcript(full_text)

        updated = await self._organizer.refresh_preview()
        if updated:
            await self._emit_notes_update()
        self._progress_message = ""

    # ── Pass 1: full Deepgram + speaker summaries + classification ───────────

    async def _post_session_pass1(self) -> None:
        from src.session.storage import write_pass1_json, write_transcript_only

        loop = asyncio.get_event_loop()
        tmp_dir = Path(self._cfg["output"]["tmp_directory"])
        sample_rate = self._cfg["audio"]["sample_rate"]

        try:
            self._progress_message = "Concatenating session audio…"
            concat_path = tmp_dir / "session_full.wav"
            valid_chunks = [p for p in self._all_chunk_paths if p.exists()]

            if not valid_chunks:
                logger.warning("No audio chunks — nothing to process")
                self._progress_message = "No audio captured"
                self._state = SessionState.IDLE
                return

            await loop.run_in_executor(
                None, lambda: concatenate_audio_chunks(valid_chunks, concat_path, sample_rate),
            )

            # Full Deepgram pass — diarization + keyterm boost
            self._progress_message = "Transcribing full session (with speakers)…"
            keyterms = self._campaign.keyterms() if self._campaign else []
            try:
                result = await loop.run_in_executor(
                    None, lambda: transcribe_full(concat_path, keyterms),
                )
            except Exception as exc:
                logger.error("Full transcription failed: %s", exc)
                self._progress_message = f"Transcription failed: {exc}"
                self._state = SessionState.IDLE
                return

            # Convert utterances → TranscriptLines
            self._canonical_transcript = []
            for u in result.utterances:
                speaker_id = f"SPEAKER_{(u.speaker or 0):02d}"
                label = self._speaker_labels.get(speaker_id, default_speaker_label(speaker_id))
                line = TranscriptLine(
                    start=u.start,
                    end=u.end,
                    speaker_id=speaker_id,
                    speaker_label=label,
                    text=u.text,
                )
                self._canonical_transcript.append(line)
                for cb in self._on_transcript_line:
                    try:
                        await cb(line)
                    except Exception:
                        pass
            self._diar_version += 1

            # Guard: no speech detected → skip the labeling dance entirely.
            # The most common cause is audio not actually flowing through BlackHole
            # (Discord output not routed to a Multi-Output Device that includes
            # BlackHole), so every chunk is silence.
            if not self._canonical_transcript:
                logger.warning("Transcription returned zero utterances — no speech detected")
                self._progress_message = (
                    "No speech detected. Check that Discord output is routed to "
                    "a Multi-Output Device that includes BlackHole 2ch."
                )
                self._state = SessionState.IDLE
                return

            # Write transcript to disk NOW — before Pass 1, so we always have it
            if self._session_dir:
                write_transcript_only(self._session_dir, self._canonical_transcript)

            # Gemini Pass 1 — speaker summaries + classification
            self._progress_message = "Identifying speakers…"
            transcript_lines = _render_transcript_with_indices(self._canonical_transcript)
            pass1 = await self._organizer.run_pass1(transcript_lines)

            if pass1 is None:
                logger.error("Pass 1 failed — no summaries produced")
                # Still advance to AWAITING_LABELS so the user can label from raw transcript
                pass1 = self._synthesize_pass1_fallback()

            # Enrich speaker summaries with actual utterance counts + seconds
            _enrich_speaker_stats(pass1, self._canonical_transcript)
            self._pass1_result = pass1

            if self._session_dir:
                write_pass1_json(self._session_dir, pass1)

            self._state = SessionState.AWAITING_LABELS
            self._progress_message = "Ready for speaker labels"
            for cb in self._on_pass1_ready:
                try:
                    await cb()
                except Exception:
                    pass
            logger.info(
                "Pass 1 complete — %d speakers identified, awaiting labels (%s)",
                len(pass1.speakers), self._session_dir.name if self._session_dir else "?",
            )

        except Exception as exc:
            logger.exception("Pass 1 processing failed: %s", exc)
            self._progress_message = f"Error: {exc}"
            self._state = SessionState.IDLE

    def _synthesize_pass1_fallback(self) -> Pass1Result:
        """If Gemini Pass 1 fails, build a minimal Pass1Result from the raw transcript."""
        from src.notes.models import SpeakerSummary, UtteranceTag
        speaker_ids = sorted({ln.speaker_id for ln in self._canonical_transcript})
        speakers = [
            SpeakerSummary(
                speaker_id=sid,
                summary="(Pass 1 failed — label manually from transcript context)",
                role_guess="unknown",
            )
            for sid in speaker_ids
        ]
        tags = [UtteranceTag(index=i, tag="in_character") for i in range(len(self._canonical_transcript))]
        return Pass1Result(speakers=speakers, tags=tags)

    # ── Pass 2: labeled + filtered notes extraction + save + campaign merge ──

    async def _post_session_pass2(self) -> None:
        from src.session.storage import save_session

        try:
            if not self._canonical_transcript:
                logger.warning("Pass 2 called with empty transcript")
                self._state = SessionState.IDLE
                return

            self._progress_message = "Generating session notes…"

            # Filter by in_character tags (with safety fallback)
            filtered_transcript = _filter_transcript_by_tags(
                self._canonical_transcript,
                self._pass1_result,
            )

            transcript_for_llm = _transcript_as_text(filtered_transcript)
            self._organizer.update_transcript(transcript_for_llm)
            ok = await self._organizer.run_pass2(transcript_for_llm)
            if not ok:
                logger.warning("Pass 2 produced no notes")

            await self._emit_notes_update()

            # Save + merge into campaign
            self._progress_message = "Saving session…"
            tmp_dir = Path(self._cfg["output"]["tmp_directory"])
            save_session(
                session_dir=self._session_dir,
                transcript=self._canonical_transcript,
                notes=self._organizer.get_notes(),
                auto_delete_audio=self._cfg["output"]["auto_delete_audio"],
                tmp_dir=tmp_dir,
            )

            if self._campaign and self._session_dir:
                from src.campaign.storage import merge_session_into_campaign, save_campaign
                updated = merge_session_into_campaign(
                    self._campaign,
                    self._organizer.get_notes(),
                    session_id=self._session_dir.name,
                )
                save_campaign(updated)
                logger.info("Session merged into campaign %s", updated.id)

            self._progress_message = "Done"
            logger.info("Session saved to %s", self._session_dir)

        except Exception as exc:
            logger.exception("Pass 2 processing failed: %s", exc)
            self._progress_message = f"Error: {exc}"
        finally:
            self._state = SessionState.IDLE

    # ── Helpers ───────────────────────────────────────────────────────────────

    async def _emit_notes_update(self) -> None:
        for cb in self._on_notes_update:
            try:
                await cb()
            except Exception as exc:
                logger.warning("on_notes_update callback failed: %s", exc)

    async def _auto_save_loop(self) -> None:
        """Periodic checkpoint of preview notes during a live session."""
        while self._state == SessionState.RUNNING:
            try:
                await asyncio.sleep(300)
                if self._state != SessionState.RUNNING:
                    break
                if not self._session_dir:
                    continue
                # Lightweight preview checkpoint — writes a draft notes.md next to the
                # audio chunks so the user has something if the app crashes mid-session.
                # Post-session Pass 2 will overwrite this with the canonical notes.
                from src.session.storage import _write_notes
                try:
                    _write_notes(self._session_dir / "notes.draft.md", self._organizer.get_notes())
                except Exception:
                    pass
                logger.info("Auto-save checkpoint → %s", self._session_dir)
            except asyncio.CancelledError:
                break
            except Exception as exc:
                logger.warning("Auto-save failed: %s", exc)

    async def _health_check_loop(self) -> None:
        while self._state == SessionState.RUNNING:
            try:
                await asyncio.sleep(5)
                if self._state != SessionState.RUNNING:
                    break
                if not self._capture.is_running:
                    logger.error("Audio device disconnected mid-session!")
                    self._progress_message = "WARNING: Audio device disconnected!"
                    break
            except asyncio.CancelledError:
                break
            except Exception:
                pass


# ── Module-level helpers ──────────────────────────────────────────────────────


def _render_transcript_with_indices(lines: list[TranscriptLine]) -> list[str]:
    """Render transcript lines with their 0-based index as a bracketed prefix, for Pass 1 prompting."""
    return [
        f"[{i}] {ln.speaker_id}: {ln.text}"
        for i, ln in enumerate(lines)
    ]


def _enrich_speaker_stats(pass1: Pass1Result, transcript: list[TranscriptLine]) -> None:
    """Fill in utterance_count + total_seconds on each SpeakerSummary from the real transcript."""
    counts: dict[str, int] = {}
    seconds: dict[str, float] = {}
    for ln in transcript:
        counts[ln.speaker_id] = counts.get(ln.speaker_id, 0) + 1
        seconds[ln.speaker_id] = seconds.get(ln.speaker_id, 0.0) + max(0.0, ln.end - ln.start)
    known_ids = {s.speaker_id for s in pass1.speakers}
    for s in pass1.speakers:
        s.utterance_count = counts.get(s.speaker_id, s.utterance_count)
        s.total_seconds = seconds.get(s.speaker_id, s.total_seconds)
    # Add any speaker_ids that appeared in the transcript but Pass 1 missed
    from src.notes.models import SpeakerSummary
    for sid in sorted(counts.keys() - known_ids):
        pass1.speakers.append(SpeakerSummary(
            speaker_id=sid,
            utterance_count=counts[sid],
            total_seconds=seconds[sid],
            summary="(not summarized — label from transcript context)",
            role_guess="unknown",
        ))


def _filter_transcript_by_tags(
    transcript: list[TranscriptLine],
    pass1: Optional[Pass1Result],
) -> list[TranscriptLine]:
    """
    Return only in_character utterances. If >70% of lines are tagged other,
    assume the classifier misfired and return the full transcript (tags become
    display-only).
    """
    if pass1 is None or not pass1.tags:
        return transcript
    tag_by_idx = {t.index: t.tag for t in pass1.tags}
    in_char_count = sum(1 for i in range(len(transcript)) if tag_by_idx.get(i, "in_character") == "in_character")
    if len(transcript) == 0 or in_char_count / len(transcript) < (1.0 - _CLASSIFIER_SAFETY_THRESHOLD):
        logger.warning(
            "Classifier safety tripped — %d/%d lines tagged in_character; running Pass 2 on full transcript",
            in_char_count, len(transcript),
        )
        return transcript
    return [
        ln for i, ln in enumerate(transcript)
        if tag_by_idx.get(i, "in_character") == "in_character"
    ]


def _transcript_as_text(lines: list[TranscriptLine]) -> str:
    """Render transcript lines as a plain text block for the LLM."""
    parts = []
    for line in lines:
        parts.append(f"[{_fmt_time(line.start)}] {line.speaker_label}: {line.text}")
    return "\n".join(parts)


def _fmt_time(seconds: float) -> str:
    m, s = divmod(int(seconds), 60)
    h, m = divmod(m, 60)
    if h:
        return f"{h}:{m:02d}:{s:02d}"
    return f"{m:02d}:{s:02d}"
