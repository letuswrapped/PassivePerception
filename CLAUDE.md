# Passive Perception — Claude Context

Local D&D session scribe for macOS. Captures Discord audio via BlackHole, transcribes with MLX Whisper, diarizes speakers post-session, organizes notes with a local LLM (Ollama + qwen3:4b), and auto-exports to an Obsidian vault. Everything runs on-device — no cloud APIs, no tokens.

## Session start

1. Read the latest note in `~/Documents/Obsidian/PassivePerception/sessions/` — it'll have the most recent decisions, tech debt, and follow-ups.
2. Check `.codesight/CODESIGHT.md` before exploring the codebase — it has routes, libs, and env vars pre-indexed (~20K tokens saved vs scanning source).
3. For subsystem details, check `.serena/memories/` if present.

## Hard rules

- **Never commit or push without asking.** This is a solo project and I want to see the diff first.
- **Never `git push --force` to `main`.** Use branches if rewrite is actually needed.
- **Never edit files in `sessions/`, `tmp/`, `pretrained_models/`, or `swift-diarizer/.build/`** — all user data or build artifacts, gitignored for a reason.
- **Never add a cloud API or telemetry.** The whole point of this app is local-first. If you think cloud is the right answer for something, surface it as a proposal, not a change.
- **Never re-introduce the HuggingFace token requirement.** We migrated off pyannote specifically to drop it.
- **Escalate early.** If a task needs a decision (architecture, tradeoff, spend, dependency choice), ask rather than picking silently.

## Tech stack

- **Python 3.11** via pyenv. Not 3.12+, not 3.14 — ML wheels aren't there yet.
- **Transcription:** `mlx-whisper` on Apple Silicon (Neural Engine via MLX), `faster-whisper` fallback.
- **Diarization:** FluidAudio Swift CLI (`swift-diarizer/` → `DiarizeCLI`) primary, `simple-diarizer` (ECAPA-TDNN) Python fallback. Runs in a subprocess so memory is reclaimed on exit.
- **LLM for notes:** Ollama + `qwen3:4b`. Structured output via `format=SessionNotes.model_json_schema()`. Runs as a separate `ollama serve` process.
- **Audio:** `sounddevice` for capture. BlackHole 2ch for Discord, optional secondary mic stream mixed in for the local player.
- **Backend:** FastAPI + websockets. Served by uvicorn in a background thread.
- **Frontend:** Vanilla HTML/CSS/JS. No build step. Dark D&D-adjacent theme.
- **Window:** pywebview native frameless macOS window.
- **Packaging:** `build_macos.sh` → signed + notarized `.app` + DMG. Identity is `Developer ID Application: Colby Schenck (D9L2AS7SDJ)`, notary profile `PassivePerception`.

## Architecture — the load-bearing idea

Two pipelines, not one. Memory budget is the constraint (16 GB RAM on the M5).

**Live pipeline (during a session):**
```
BlackHole + mic → AudioCapture → AudioBuffer (30s chunks) → WAV file
  → TranscriptionEngine (MLX Whisper) → TranscriptLine[]
  → NoteOrganizer.update_transcript (just stores lines, no LLM yet)
```
The live LLM loop runs lightweight periodic passes every `update_interval` seconds on the tail of the transcript so the UI shows *something* updating. The real work happens post-session.

**Post-session pipeline (after Stop):**
```
Unload Whisper (gc + mx.metal.clear_cache) → concatenate chunks →
  diarize full audio in subprocess → relabel lines →
  full chunked LLM pass over entire transcript →
  save transcript.md + notes.md → auto-export to Obsidian → delete WAVs
```
The Whisper unload is **required** — Whisper + Ollama + diarization can't all be resident on 16 GB. Subprocessing diarization ensures its memory is freed when the worker exits.

## Key patterns & invariants

- **Memory discipline.** Every long-lived ML model gets an unload point. If you add a new model, decide upfront when it's freed. Don't assume Python GC will do it — MLX needs `mx.metal.clear_cache()` to release GPU memory.
- **Chunked LLM passes.** 300 lines per chunk (~6K tokens). Each chunk gets the accumulated notes so far as prior context. See `src/notes/organizer.py::_sync_chunked_pass`. Don't try to feed the whole transcript at once.
- **JSON repair.** LLMs occasionally truncate output. `_repair_truncated_json` salvages partial responses by closing open brackets. When you see an LLM-output parse, check it handles this.
- **String → object coercion.** Models sometimes return `["Gorzav"]` instead of `[{"name": "Gorzav"}]` for NPCs/locations. `_parse_response` coerces these. Keep it.
- **Auto-save every 5 min during a live session** (`_auto_save_loop` in `manager.py`) plus `_emergency_save` in `run.py` atexit hook. Sessions longer than 3 hours are the whole reason this exists — don't break the checkpointing.
- **Obsidian export is opt-in but lives in `save_session`.** A missing or invalid `obsidian_config.json` is not an error — it's a no-op with a warning log. Preserve that behavior.
- **Speaker labels.** During live, everything is `SPEAKER_00` / "Speaker 1". Diarization re-labels at the end. The UI supports inline rename that updates all lines with that `speaker_id`.

## Running the app

```bash
source .venv/bin/activate
python run.py                 # opens native window
```

Prerequisites the launcher checks: BlackHole installed, `simple-diarizer` importable, Ollama binary on PATH, `qwen3:4b` model pulled.

## Build + release checklist

Before running `./build_macos.sh`:
1. Bump `VERSION` in `build_macos.sh` if shipping to users.
2. Confirm `security find-identity -v -p codesigning` shows the Developer ID cert.
3. Confirm `xcrun notarytool history --keychain-profile PassivePerception` runs without error (notary creds present).
4. Run the app end-to-end once (5-min recording → stop → confirm transcript + notes are saved + Obsidian exports).

The build script handles signing (hardened runtime + entitlements), DMG creation, notarization with `--wait`, and stapling. Don't split those steps — do them atomically.

## Post-session writeup

At the end of a dev session, run the `/notes` slash command to write a session note to `~/Documents/Obsidian/PassivePerception/sessions/YYYY-MM-DD.md`. The vault also has `decisions/` for ADRs when we make a meaningful tradeoff; use `decisions/template.md` as the starting point.

## Known landmines

- **Rosetta detection.** `sysctl -n machdep.cpu.brand_string` is the correct check, not `uname -m` (which lies under Rosetta). See the `check_prerequisites` path — don't "simplify" this.
- **pywebview frameless window.** `frameless=True, easy_drag=True` is deliberate. The title bar is custom in CSS. Changing frameless will break the window-drag affordance.
- **ollama `format=schema` vs thinking mode.** Qwen3 has a thinking mode that consumed all tokens during testing. Using `format=_OUTPUT_SCHEMA` bypasses it. Do NOT remove the `format` param.
- **`POST /settings/hf-token` route.** Dead code — left in from the pyannote era. Safe to remove; flagged here so you don't wonder why it exists.
- **The `mic` stream mixes into the BlackHole stream.** If you refactor audio, preserve the thread-safe buffer + clipping in `AudioCapture`. Length mismatches between streams are expected; don't assume alignment.

## File layout (at-a-glance)

```
src/
  app.py                      # FastAPI routes + websockets + state
  audio/
    capture.py                # Dual-source mixing (BlackHole + mic)
    buffer.py                 # 30s chunk accumulator
    devices.py                # Enumeration, loopback detection
    backends/{macos,windows}.py
  transcription/
    engine.py                 # MLX Whisper + faster-whisper fallback
    diarization.py            # Entry point, spawns worker subprocess
    diarization_worker.py     # Runs FluidAudio CLI or simple-diarizer
  notes/
    organizer.py              # Live + chunked Ollama passes, merge, repair
    models.py                 # Pydantic: SessionNotes, NPC, Location, ...
    prompts.py                # System prompt + message builder
  session/
    manager.py                # Lifecycle, post-session pipeline
    storage.py                # transcript.md, notes.md, Obsidian export
  ui/static/                  # index.html + app.js + style.css (no build)

swift-diarizer/               # Swift Package — compiles to DiarizeCLI
build_macos.sh                # .app + DMG + sign + notarize + staple
setup.sh                      # One-shot dev environment setup
run.py                        # Entry point, native window, emergency save
config.yaml                   # User-facing runtime config
```

## References

- `.codesight/CODESIGHT.md` — auto-generated route/lib index
- `.codesight/routes.md`, `libs.md`, `config.md` — expanded per-section
- `.serena/memories/` — deeper per-subsystem notes (add as needed)
- `~/Documents/Obsidian/PassivePerception/sessions/` — dated session notes
- `~/Documents/Obsidian/PassivePerception/decisions/` — ADRs
