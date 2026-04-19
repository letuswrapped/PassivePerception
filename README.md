# Passive Perception

A local macOS app that listens to your D&D sessions, transcribes the conversation with speaker identification, and automatically organizes everything into structured campaign notes — NPCs, locations, plot points, and open questions.

All processing happens on your machine. No cloud APIs, no subscriptions, no account tokens. Audio is deleted after transcription; only your notes persist.

![Passive Perception Icon](src/ui/static/icon.svg)

## What It Does

1. **Captures audio** from Discord (or any app) via the BlackHole virtual audio driver
2. **Transcribes speech** using Whisper (via MLX on Apple Silicon's Neural Engine)
3. **Identifies speakers** post-session using FluidAudio — fully local, no HuggingFace account required
4. **Generates structured notes** with a local LLM (Ollama + `qwen3:4b`) — NPCs, locations, plot points, open questions
5. **Saves everything** as clean markdown and auto-exports to your Obsidian vault if configured

## Requirements

- **macOS** with **Apple Silicon** (M1/M2/M3/M4/M5)
- **16 GB RAM** recommended
- **Python 3.11** (the setup script handles this via pyenv)
- **Homebrew** ([install here](https://brew.sh))

No cloud accounts or API tokens needed.

## Install

1. Download **PassivePerception-1.1.3.dmg** from the [latest release](https://github.com/letuswrapped/PassivePerception/releases/latest)
2. Open the DMG and drag **Passive Perception** to your Applications folder
3. Launch it — the app is signed and notarized, so macOS will open it without warnings
4. On first launch, the app self-heals any missing dependencies (ffmpeg, Ollama, the `qwen3:4b` model)

## Quick Start (from source)

If you prefer to run from source instead of the .app:

```bash
# Clone the repo
git clone https://github.com/letuswrapped/PassivePerception.git
cd PassivePerception

# Run the setup script (installs Python 3.11, dependencies, BlackHole, Ollama, etc.)
chmod +x setup.sh
./setup.sh

# Start the app
source .venv/bin/activate
python run.py
```

The app opens in a native frameless macOS window.

## Audio Setup

Passive Perception captures audio through **BlackHole 2ch**, a virtual audio driver. The setup script installs it, but you need to create a Multi-Output Device so you can hear your Discord call AND have the app capture it:

1. Open **Audio MIDI Setup** (Applications > Utilities)
2. Click **+** at bottom-left, select **Create Multi-Output Device**
3. Check both your **headphones/speakers** and **BlackHole 2ch**
4. Right-click the new device, select **Use This Device For Sound Output**
5. In Discord: Settings > Voice & Video > Output Device > **Multi-Output Device**

## How to Use

1. Join your Discord session as usual
2. Open Passive Perception and click **Begin Session**
3. The transcript appears in real-time on the left panel; lightweight note passes update the right panel periodically
4. Click **Stop** when your session ends — the app runs full diarization and a chunked LLM pass over the entire transcript, then saves final notes
5. Transcript and notes are written as markdown and auto-exported to Obsidian if configured

### Player Setup

On first launch, you can enter your player name, character details, and backstory. This helps the AI identify you in the transcript and generate more relevant notes.

### Obsidian Export

Drop an `obsidian_config.json` next to the app pointing at your vault and each session auto-exports on Stop. If the file is missing or invalid, export is silently skipped — nothing else breaks.

## Configuration

Edit `config.yaml` to customize:

```yaml
audio:
  device: "BlackHole 2ch"    # auto-detected if this exact name is found
  chunk_duration: 8          # seconds per audio chunk

transcription:
  model: "small.en"          # tiny.en, base.en, small.en, medium.en, large-v3

diarization:
  threshold: 0.8             # clustering sensitivity (lower = more speakers)

notes:
  llm_model: "qwen3:4b"      # Ollama model
  update_interval: 300       # seconds between live note passes
```

## Architecture

Two pipelines, memory-budgeted for 16 GB:

- **Live:** BlackHole + mic → chunked WAVs → MLX Whisper → transcript lines. Lightweight LLM passes run on the tail of the transcript so the UI shows progress.
- **Post-session:** Whisper unloads (freeing GPU memory), full audio is diarized in a subprocess, a chunked LLM pass organizes the whole transcript, and everything saves to markdown.

## Project Structure

```
PassivePerception/
├── run.py                    # Entry point, native window
├── config.yaml               # User configuration
├── setup.sh                  # One-command dev setup
├── build_macos.sh            # Signed + notarized .app + DMG
├── src/
│   ├── app.py                # FastAPI routes + WebSocket
│   ├── audio/                # Capture, buffering, device enumeration
│   ├── transcription/        # MLX Whisper + diarization worker
│   ├── notes/                # Ollama passes, JSON repair, merge logic
│   ├── session/              # Lifecycle, storage, Obsidian export
│   └── ui/static/            # Frontend — no build step
├── swift-diarizer/           # FluidAudio Swift CLI (compiles to DiarizeCLI)
└── sessions/                 # Saved session data (gitignored)
```

## Tech Stack

- **Transcription:** [mlx-whisper](https://github.com/ml-explore/mlx-examples) on the Apple Neural Engine, with `faster-whisper` as CPU fallback
- **Speaker ID:** [FluidAudio](https://github.com/FluidInference/FluidAudio) Swift CLI (primary), `simple-diarizer` Python fallback — all local, no HF token
- **Notes LLM:** [Ollama](https://ollama.com) running `qwen3:4b` with structured JSON output
- **Audio:** [sounddevice](https://python-sounddevice.readthedocs.io/) + BlackHole 2ch
- **Server:** [FastAPI](https://fastapi.tiangolo.com/) + WebSockets, served by uvicorn
- **Window:** [pywebview](https://pywebview.flowrl.com/) frameless native macOS window
- **Frontend:** Vanilla HTML/CSS/JS — no build step

## License

MIT
