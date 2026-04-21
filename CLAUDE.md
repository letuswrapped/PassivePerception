# Passive Perception — Claude Context

Cloud-backed D&D session scribe for macOS. Captures Discord audio via BlackHole, transcribes + diarizes via Deepgram Nova-3, extracts structured notes via Google Gemini 2.5 Flash, and auto-exports to an Obsidian vault. A persistent per-campaign roster (PCs, NPCs, locations, plot threads) biases both Deepgram (fantasy-name recognition) and Gemini (entity continuity + player perspective) so each session builds on the last.

## You are on the `windows` branch

The macOS build lives on `main` and is frozen at its latest release — do **not** pull Windows changes back to main without explicit user approval. Windows work is additive: `src/platform_utils.py`, `src/audio/backends/windows.py`, and `sys.platform`-guarded branches in `run.py` / `src/cloud_config.py` / `src/campaign/storage.py` / `src/app.py`. macOS behavior on this branch must remain bit-identical to the main branch — if you touch shared code, prove with a smoke test (import + route count + path resolution) that macOS output hasn't changed. See README's "Windows (alpha)" section for the Phase-0 scope and known gaps.

## Session start

1. Read the latest note in `~/Documents/Obsidian/PassivePerception/sessions/` — it'll have the most recent decisions, tech debt, and follow-ups.
2. Check `.codesight/CODESIGHT.md` before exploring the codebase — it has routes, libs, and env vars pre-indexed.
3. For subsystem details, check `.serena/memories/` if present.

## Hard rules

- **Never commit or push without asking.** Solo project — I want to see the diff first.
- **Never `git push --force` to `main`.** Use branches if a rewrite is really needed.
- **Never edit files in `sessions/`, `tmp/`, or `pretrained_models/`** — user data or leftover model weights from the old local stack, gitignored for a reason.
- **Never put API keys in the repo or in `config.yaml`.** Keys live in `~/Library/Application Support/Passive Perception/.env` only, managed via `src/cloud_config.py` and the Settings → API Keys UI.
- **Never send audio or transcripts to a provider without `mip_opt_out=True` (Deepgram) or the paid no-training tier (Gemini).** Zero-retention was a hard requirement of the cloud migration.
- **Escalate early.** If a task needs a decision (architecture, tradeoff, spend, dependency choice), ask rather than picking silently.

## Tech stack

- **Python 3.11** via pyenv.
- **Transcription + diarization:** Deepgram Nova-3 via `deepgram-sdk`. Single API call returns word/utterance timestamps plus speaker labels. `keyterm` parameter is fed the campaign roster so fantasy proper nouns come out correctly. `mip_opt_out=True` opts out of Deepgram's Model Improvement Program (no retention for training).
- **Notes LLM:** `gemini-2.5-flash` via `google-genai`. Structured output via Pydantic `response_schema=SessionNotes`. 1M-token context — the whole transcript fits in a single shot, no chunking machinery.
- **Audio:** `sounddevice` for capture. BlackHole 2ch for Discord, optional secondary mic stream mixed in for the local player.
- **Backend:** FastAPI + websockets. Served by uvicorn in a background thread.
- **Frontend:** Vanilla HTML/CSS/JS. No build step. Dark D&D-adjacent theme.
- **Window:** pywebview native frameless macOS window.
- **Packaging:** `build_macos.sh` → signed + notarized `.app` + DMG. Identity is `Developer ID Application: Colby Schenck (D9L2AS7SDJ)`, notary profile `PassivePerception`.

## Architecture — the load-bearing idea

**Campaign roster is the quality lever.** A persistent per-campaign JSON file (stored in the Obsidian vault under `PassivePerception/campaigns/<slug>.json`) holds the user's own character, the party, known NPCs and locations, active plot threads, and a "last time on this campaign" state recap. On every session:

1. The roster's name list becomes Deepgram's `keyterm` — biasing transcription of every fantasy proper noun.
2. The roster becomes part of Gemini's system prompt — biasing extraction (no duplicate NPCs, continuous plot threads across sessions) and biasing summarization toward what matters to the user (their character's goals, their backstory hooks, interactions involving them).
3. After the session, the extracted notes are merged back into the roster (new NPCs appended, known ones' `last_session` refreshed, open questions added as unresolved hooks) so the roster grows with play.

**Live session:** audio-only.
```
BlackHole + mic → AudioCapture → AudioBuffer (30s WAV chunks) → disk
```
No transcription happens during capture — chunk files accumulate on disk. Every `notes.update_interval` seconds (default 900 = 15 min), a preview cycle runs:
```
concatenate new chunks → Deepgram preview (no diarization) →
  append to internal transcript → Gemini preview pass → UI notes update
```
The preview transcript is LLM context only — it's never displayed or saved. The user sees the notes panel refresh; the canonical transcript appears post-session.

**Post-session (after Stop):**
```
concatenate all chunks → session_full.wav →
  Deepgram full (diarize=True + keyterm boost) → canonical TranscriptLine[] →
  Gemini full pass → canonical SessionNotes →
  save transcript.md + notes.md → Obsidian export →
  merge extracted entities into active campaign → delete WAVs
```

## Key patterns & invariants

- **Zero-retention flags are non-negotiable.** `mip_opt_out=True` on every Deepgram call (`src/transcription/deepgram_client.py::_common_params`). If you see a call without it, that's a bug. Gemini paid-tier is no-training by default.
- **Campaign roster fuels everything.** Both clients accept a `campaign` object; `campaign.keyterms()` → Deepgram, `build_system_prompt(campaign)` → Gemini. If you add new extraction behavior, wire it to the campaign.
- **Single timer owns pacing.** The preview cadence is driven by `SessionManager._preview_loop`. The `NoteOrganizer` is stateless about timing — it only exposes `refresh_preview()` / `run_full_pass()`. Do not re-introduce a second timer inside the organizer.
- **Gemini structured output.** `response_mime_type='application/json'` + `response_schema=SessionNotes` gives validated Pydantic output with no JSON-repair dance. Do not fall back to free-form JSON prompting.
- **Auto-save every 5 min during a live session** (`_auto_save_loop` in `manager.py`) plus `_emergency_save` in `run.py` atexit hook. Notes panel can have value even if the session crashes mid-way.
- **Obsidian export is opt-in but lives in `save_session`.** A missing or invalid `obsidian_config.json` is not an error — it's a no-op with a warning log. Preserve that behavior.
- **Speaker labels.** Deepgram returns integer speakers which we format as `SPEAKER_00` etc. with `default_speaker_label` → "Speaker 1". The UI supports inline rename that updates all lines with that `speaker_id`.

## Running the app

```bash
source .venv/bin/activate
python run.py                 # opens native window
```

Prerequisites: BlackHole installed, Deepgram + Gemini API keys entered in Settings, an active campaign selected.

## Build + release checklist

Before running `./build_macos.sh`:
1. Bump `VERSION` in `build_macos.sh` if shipping to users.
2. Confirm `security find-identity -v -p codesigning` shows the Developer ID cert.
3. Confirm `xcrun notarytool history --keychain-profile PassivePerception` runs without error (notary creds present).
4. Run the app end-to-end once (5-min recording → stop → confirm transcript + notes + Obsidian export + campaign roster merge).

The build script handles signing (hardened runtime + entitlements), DMG creation, notarization with `--wait`, and stapling. Don't split those steps — do them atomically.

## Post-session writeup

At the end of a dev session, run the `/notes` slash command to write a session note to `~/Documents/Obsidian/PassivePerception/sessions/YYYY-MM-DD.md`. The vault also has `decisions/` for ADRs when we make a meaningful tradeoff; use `decisions/template.md` as the starting point.

## Known landmines

- **Rosetta detection.** `sysctl -n machdep.cpu.brand_string` is the correct check, not `uname -m` (which lies under Rosetta). Don't "simplify" this.
- **pywebview frameless window.** `frameless=True, easy_drag=False` + topbar-scoped drag monkey-patch in `run.py`. Changing frameless breaks the unified titlebar look.
- **Don't try `-webkit-app-region: drag` in CSS.** WKWebView ignores it — it's an Electron-only feature. The correct pattern is the `performWindowDragWithEvent_` monkey-patch in `run.py`.
- **The `mic` stream mixes into the BlackHole stream.** If you refactor audio, preserve the thread-safe buffer + clipping in `AudioCapture`. Length mismatches between streams are expected.
- **Campaign roster file format is the contract.** UI and backend both round-trip through `Campaign.model_validate_json`. Adding a field is safe (Pydantic tolerates extras in older files); renaming or removing one will break saved campaigns.

## File layout (at-a-glance)

```
src/
  app.py                         # FastAPI routes + websockets + state
  cloud_config.py                # Deepgram/Gemini API key storage (.env in Application Support)
  audio/
    capture.py                   # Dual-source mixing (BlackHole + mic)
    buffer.py                    # 30s WAV chunk accumulator
    devices.py                   # Enumeration, loopback detection
    backends/{macos,windows}.py
  campaign/
    models.py                    # Pydantic: Campaign, CampaignCharacter, NPC, Location, PlotThread, State
    storage.py                   # Per-campaign JSON files + active-campaign pointer + session merge
  transcription/
    __init__.py                  # TranscriptLine dataclass, default_speaker_label()
    deepgram_client.py           # Deepgram Nova-3 wrapper (preview + full passes)
    audio_utils.py               # WAV concatenation for post-session
  notes/
    organizer.py                 # Gemini passes (refresh_preview + run_full_pass)
    models.py                    # Pydantic: SessionNotes, NPC, Location, PlotPoint
    prompts.py                   # System prompt builder incorporating campaign + player perspective
  session/
    manager.py                   # Lifecycle — preview loop, post-session pipeline, campaign merge
    storage.py                   # transcript.md, notes.md, Obsidian export
  ui/static/                     # index.html + app.js + style.css (no build)

build_macos.sh                   # .app + DMG + sign + notarize + staple
setup.sh                         # One-shot dev environment setup
run.py                           # Entry point, native window, emergency save
config.yaml                      # Non-secret runtime config
```

## References

- `.codesight/CODESIGHT.md` — auto-generated route/lib index
- `~/Documents/Obsidian/PassivePerception/sessions/` — dated session notes
- `~/Documents/Obsidian/PassivePerception/decisions/` — ADRs
- `~/Documents/Obsidian/PassivePerception/campaigns/` — persistent campaign rosters (created at runtime)
