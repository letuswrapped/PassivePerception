# Passive Perception

A local macOS app that listens to your D&D sessions, transcribes the conversation with speaker identification, and automatically organizes everything into structured campaign notes — NPCs, locations, plot points, and open questions.

All processing happens on your machine. No cloud APIs, no subscriptions. Audio is deleted after transcription; only your notes persist.

![Passive Perception Icon](src/ui/static/icon.svg)

## What It Does

1. **Captures audio** from Discord (or any app) via BlackHole virtual audio driver
2. **Transcribes speech** in real-time using Whisper (via MLX on Apple Silicon)
3. **Identifies speakers** using pyannote-audio so you know who said what
4. **Generates structured notes** using a local LLM — NPCs, locations, plot points, open questions
5. **Saves everything** as clean markdown files you can drop into Obsidian, Notion, or wherever

## Requirements

- **macOS** with **Apple Silicon** (M1/M2/M3/M4/M5)
- **16 GB RAM** recommended
- **Python 3.11** (the setup script handles this via pyenv)
- **Homebrew** ([install here](https://brew.sh))
- **HuggingFace account** with a [read token](https://huggingface.co/settings/tokens) — needed for the speaker diarization model

## Quick Start

```bash
# Clone the repo
git clone https://github.com/YOUR_USERNAME/PassivePerception.git
cd PassivePerception

# Run the setup script (installs Python 3.11, dependencies, BlackHole, etc.)
chmod +x setup.sh
./setup.sh

# Start the app
source .venv/bin/activate
python run.py
```

The app opens in your browser at `http://localhost:8000`.

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
3. The transcript appears in real-time on the left panel
4. Every 5 minutes, the AI generates/updates structured notes on the right panel
5. Click **Stop** when your session ends — final notes are saved
6. Export transcript or notes as markdown files using the **Export** buttons

### Player Setup

On first launch, you can enter your player name, character details, and backstory. This helps the AI identify you in the transcript and generate more relevant notes.

## Configuration

Edit `config.yaml` to customize:

```yaml
audio:
  device: "BlackHole 2ch"    # Audio input device
  chunk_duration: 8          # Seconds per audio chunk

transcription:
  model: "small.en"          # Whisper model size (tiny.en, base.en, small.en, medium.en)

notes:
  llm_model: "mlx-community/Llama-3.2-3B-Instruct-4bit"
  update_interval: 300       # Seconds between note generation passes
```

## Project Structure

```
PassivePerception/
├── setup.sh                  # One-command setup
├── run.py                    # Entry point
├── config.yaml               # User configuration
├── requirements.txt
├── src/
│   ├── app.py                # FastAPI server + WebSocket
│   ├── audio/                # Audio capture & buffering
│   ├── transcription/        # Whisper + speaker diarization
│   ├── notes/                # LLM prompt & note generation
│   ├── session/              # Session lifecycle & storage
│   └── ui/static/            # Frontend (HTML/CSS/JS)
└── sessions/                 # Saved session data (gitignored)
```

## Tech Stack

- **Transcription**: [mlx-whisper](https://github.com/ml-explore/mlx-examples) (Apple Silicon Neural Engine)
- **Speaker ID**: [pyannote-audio](https://github.com/pyannote/pyannote-audio) 4.0
- **Notes LLM**: [mlx-lm](https://github.com/ml-explore/mlx-examples) with Llama 3.2 3B
- **Audio**: [sounddevice](https://python-sounddevice.readthedocs.io/) + BlackHole 2ch
- **Server**: [FastAPI](https://fastapi.tiangolo.com/) + WebSockets
- **Frontend**: Vanilla HTML/CSS/JS — no build step

## License

MIT
