"""
Session lifecycle manager.

Live pipeline (during session):
  Audio → resample → buffer → MLX Whisper → transcript lines (all Speaker 1)

Post-session pipeline (after Stop):
  Concatenate audio → pyannote on MPS → relabel lines → save → delete audio
"""

from __future__ import annotations

import asyncio
import logging
from datetime import datetime
from pathlib import Path

from src.audio.buffer import AudioBuffer
from src.audio.capture import AudioCapture
from src.notes.organizer import NoteOrganizer
from src.notes.models import SessionNotes
from src.transcription.diarization import (
    TranscriptLine,
    _default_label,
    concatenate_audio_chunks,
    run_diarization,
)
from src.transcription.engine import TranscriptionEngine

logger = logging.getLogger(__name__)


class SessionState:
    IDLE        = "idle"
    RUNNING     = "running"
    PROCESSING  = "processing"   # post-session diarization in progress
    STOPPING    = "stopping"


class SessionManager:
    def __init__(self, config: dict, player_context: dict | None = None) -> None:
        self._cfg = config
        self._state = SessionState.IDLE
        self._started_at: datetime | None = None
        self._session_dir: Path | None = None
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
        self._transcriber = TranscriptionEngine(
            model_name=config["transcription"]["model"],
            language=config["transcription"]["language"],
        )
        self._organizer = NoteOrganizer(
            model=config["notes"]["llm_model"],
            update_interval=config["notes"]["update_interval"],
            player_context=player_context,
        )

        self._chunk_queue: asyncio.Queue[Path] = asyncio.Queue()
        self._transcript: list[TranscriptLine] = []
        self._chunk_paths: list[Path] = []    # kept for post-session diarization
        self._speaker_labels: dict[str, str] = {}
        self._chunk_offset: float = 0.0
        self._diar_version: int = 0           # incremented after each diarization pass

        self._on_transcript_line: list = []
        self._on_notes_update: list = []
        self._process_task: asyncio.Task | None = None
        self._transcript_lock = asyncio.Lock()

    # ── Public API ────────────────────────────────────────────────────────────

    def set_mic_device(self, device_name: str | None) -> None:
        """Set or clear the microphone for dual-source capture."""
        self._capture.set_mic_device(device_name)
        logger.info("Mic device set: %s", device_name or "(none)")

    async def start(self, session_name: str | None = None) -> None:
        if self._state != SessionState.IDLE:
            return
        self._state = SessionState.RUNNING
        self._started_at = datetime.now()
        self._progress_message = ""
        self._transcript = []
        self._chunk_paths = []
        self._chunk_offset = 0.0
        self._diar_version = 0

        name = session_name or self._started_at.strftime("%Y-%m-%d_session")
        self._session_dir = Path(self._cfg["output"]["directory"]) / name
        self._session_dir.mkdir(parents=True, exist_ok=True)

        loop = asyncio.get_event_loop()
        self._buffer.set_output_queue(self._chunk_queue, loop)
        self._buffer.start()
        self._capture.add_callback(self._buffer.feed)
        self._capture.start()

        await self._organizer.start()
        self._process_task = asyncio.create_task(self._process_chunks())
        self._autosave_task = asyncio.create_task(self._auto_save_loop())
        self._health_task = asyncio.create_task(self._health_check_loop())
        logger.info("Session started: %s", name)

    async def stop(self) -> None:
        """
        Stop the live session and kick off post-session processing.
        Returns immediately — post-session runs in the background.
        The state moves to PROCESSING until complete.
        """
        if self._state != SessionState.RUNNING:
            raise RuntimeError("No active session.")
        self._state = SessionState.STOPPING

        self._capture.stop()
        self._buffer.stop()

        # Cancel auto-save and chunk-processing loops
        for task in (getattr(self, '_health_task', None), getattr(self, '_autosave_task', None), self._process_task):
            if task:
                task.cancel()
                try:
                    await task
                except asyncio.CancelledError:
                    pass
        self._health_task = None
        self._autosave_task = None
        self._process_task = None

        # Drain any remaining chunks that arrived after cancellation
        while not self._chunk_queue.empty():
            try:
                path = self._chunk_queue.get_nowait()
                await self._transcribe_chunk(path, keep_audio=True)
            except asyncio.QueueEmpty:
                break

        # Stop the live LLM loop (full pass happens in _post_session after diarization)
        await self._organizer.stop()

        # Hand off to background post-session processing
        self._state = SessionState.PROCESSING
        asyncio.create_task(self._post_session())

    async def _post_session(self) -> None:
        """
        Background task: diarize full session audio, run full LLM pass, save, cleanup.
        """
        import gc

        loop = asyncio.get_event_loop()
        sample_rate = self._cfg["audio"]["sample_rate"]
        tmp_dir = Path(self._cfg["output"]["tmp_directory"])

        try:
            # Free Whisper model memory — transcription is done
            self._progress_message = "Freeing transcription model memory..."
            self._transcriber = None
            gc.collect()
            try:
                import mlx.core as mx
                mx.metal.clear_cache()
            except Exception:
                pass
            logger.info("Whisper model unloaded for post-session processing")

            # Step 1: Concatenate all audio chunks
            self._progress_message = "Concatenating session audio..."
            concat_path = tmp_dir / "session_full.wav"
            valid_chunks = [p for p in self._chunk_paths if p.exists()]

            if valid_chunks:
                await loop.run_in_executor(
                    None,
                    lambda: concatenate_audio_chunks(valid_chunks, concat_path, sample_rate),
                )

                # Step 2: Run diarization on full audio
                def _progress(msg: str) -> None:
                    self._progress_message = msg

                diar_cfg = self._cfg.get("diarization", {})
                transcript_snapshot = list(self._transcript)
                updated_lines = await loop.run_in_executor(
                    None,
                    lambda: run_diarization(
                        audio_path=concat_path,
                        transcript_lines=transcript_snapshot,
                        speaker_labels=self._speaker_labels,
                        progress_cb=_progress,
                        threshold=diar_cfg.get("threshold", 0.8),
                    ),
                )
                self._transcript = updated_lines
                self._diar_version += 1
            else:
                logger.warning("No audio chunks found — skipping diarization")

            # Step 3: Run full chunked LLM pass over the entire transcript
            self._progress_message = "Generating session notes (full transcript)..."
            self._organizer.update_transcript(self._transcript_as_text())
            await self._organizer.run_full_pass()
            logger.info("Full LLM pass complete")

            # Step 4: Save session files
            self._progress_message = "Saving session notes..."
            from src.session.storage import save_session
            save_session(
                session_dir=self._session_dir,
                transcript=self._transcript,
                notes=self._organizer.get_notes(),
                auto_delete_audio=self._cfg["output"]["auto_delete_audio"],
                tmp_dir=tmp_dir,
            )

            self._progress_message = "Done"
            logger.info("Session saved to %s", self._session_dir)

        except Exception as exc:
            logger.error("Post-session processing failed: %s", exc)
            self._progress_message = f"Error: {exc}"
        finally:
            self._state = SessionState.IDLE

    def rename_speaker(self, speaker_id: str, label: str) -> None:
        self._speaker_labels[speaker_id] = label
        # Iterate over a snapshot to avoid issues if list is replaced concurrently
        for line in list(self._transcript):
            if line.speaker_id == speaker_id:
                line.speaker_label = label

    def get_transcript(self) -> list[TranscriptLine]:
        return list(self._transcript)

    def get_notes(self) -> SessionNotes:
        return self._organizer.get_notes()

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

    # ── Internal pipeline ─────────────────────────────────────────────────────

    async def _process_chunks(self) -> None:
        while self._state == SessionState.RUNNING:
            try:
                path = await asyncio.wait_for(self._chunk_queue.get(), timeout=1.0)
                await self._transcribe_chunk(path, keep_audio=True)
            except asyncio.TimeoutError:
                continue
            except asyncio.CancelledError:
                break

    async def _transcribe_chunk(self, wav_path: Path, keep_audio: bool = True) -> None:
        try:
            loop = asyncio.get_event_loop()
            whisper_segs = await loop.run_in_executor(
                None, self._transcriber.transcribe, wav_path
            )

            # Track the chunk path for post-session diarization
            if keep_audio and wav_path not in self._chunk_paths:
                self._chunk_paths.append(wav_path)

            # All lines are Speaker 1 during the live session
            for seg in whisper_segs:
                if not seg.text:
                    continue
                line = TranscriptLine(
                    start=self._chunk_offset + seg.start,
                    end=self._chunk_offset + seg.end,
                    speaker_id="SPEAKER_00",
                    speaker_label="Speaker 1",
                    text=seg.text,
                )
                self._transcript.append(line)
                for cb in self._on_transcript_line:
                    await cb(line)

            self._chunk_offset += self._cfg["audio"]["chunk_duration"]
            self._organizer.update_transcript(self._transcript_as_text())

        except Exception as exc:
            logger.error("Chunk transcription failed (%s): %s", wav_path.name, exc)

    def _transcript_as_text(self) -> list[str]:
        return [
            f"[{_fmt_time(line.start)}] {line.speaker_label}: {line.text}"
            for line in self._transcript
        ]

    async def _auto_save_loop(self) -> None:
        """Periodically checkpoint transcript and notes to disk during a live session."""
        while self._state == SessionState.RUNNING:
            try:
                await asyncio.sleep(300)  # every 5 minutes
                if self._state != SessionState.RUNNING:
                    break
                if not self._transcript or not self._session_dir:
                    continue
                from src.session.storage import save_session
                save_session(
                    session_dir=self._session_dir,
                    transcript=list(self._transcript),
                    notes=self._organizer.get_notes(),
                    auto_delete_audio=False,
                )
                logger.info("Auto-save checkpoint written to %s", self._session_dir)
            except asyncio.CancelledError:
                break
            except Exception as exc:
                logger.warning("Auto-save failed: %s", exc)

    async def _health_check_loop(self) -> None:
        """Monitor audio device health during a live session."""
        while self._state == SessionState.RUNNING:
            try:
                await asyncio.sleep(5)
                if self._state != SessionState.RUNNING:
                    break
                if not self._capture.is_running:
                    logger.error("Audio device disconnected mid-session!")
                    self._progress_message = "WARNING: Audio device disconnected!"
                    # Notify UI via transcript callback with a system message
                    warning_line = TranscriptLine(
                        start=self._chunk_offset,
                        end=self._chunk_offset,
                        speaker_id="SYSTEM",
                        speaker_label="⚠ System",
                        text="[Audio device disconnected — recording may be incomplete]",
                    )
                    self._transcript.append(warning_line)
                    for cb in self._on_transcript_line:
                        await cb(warning_line)
                    break  # Stop checking — device is gone
            except asyncio.CancelledError:
                break
            except Exception:
                pass


def _fmt_time(seconds: float) -> str:
    m, s = divmod(int(seconds), 60)
    h, m = divmod(m, 60)
    if h:
        return f"{h}:{m:02d}:{s:02d}"
    return f"{m:02d}:{s:02d}"
