"""FastAPI application — REST API + WebSocket server."""

from __future__ import annotations

import json
import logging
from pathlib import Path
from typing import Any

from typing import Optional
import yaml
from dotenv import load_dotenv
from fastapi import FastAPI, WebSocket, WebSocketDisconnect
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import HTMLResponse, PlainTextResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel

from src.audio.devices import list_input_devices
from src.session.manager import SessionManager, SessionState
from src.transcription.diarization import TranscriptLine

load_dotenv()
logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

# ── Load config ───────────────────────────────────────────────────────────────
_CONFIG_PATH = Path(__file__).parent.parent / "config.yaml"
with open(_CONFIG_PATH) as f:
    CONFIG: dict = yaml.safe_load(f)

# ── App setup ─────────────────────────────────────────────────────────────────
app = FastAPI(title="Passive Perception")

# Allow pywebview / localhost origins
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_methods=["*"],
    allow_headers=["*"],
)


# ── Request models ────────────────────────────────────────────────────────────
class StartSessionRequest(BaseModel):
    session_name: Optional[str] = None


class RenameSpeakerRequest(BaseModel):
    speaker_id: str
    label: str


class PlayerContextRequest(BaseModel):
    player_name: str = ""
    char_name: str = ""
    char_race: str = ""
    char_class: str = ""
    char_subclass: str = ""
    multiclass: bool = False
    multi_class: str = ""
    multi_subclass: str = ""
    char_bio: str = ""


# ── Player context (persisted to disk) ────────────────────────────────────────
_PLAYER_CONTEXT_PATH = Path(__file__).parent.parent / "player_context.json"
_player_context: dict = {}

def _load_player_context() -> dict:
    if _PLAYER_CONTEXT_PATH.exists():
        try:
            return json.loads(_PLAYER_CONTEXT_PATH.read_text())
        except Exception:
            pass
    return {}

_player_context = _load_player_context()

# ── Mic device (persisted in-memory, applied to each new session) ────────
_mic_device: str | None = None

# ── Obsidian config (persisted to disk) ──────────────────────────────────
_OBSIDIAN_CONFIG_PATH = Path(__file__).parent.parent / "obsidian_config.json"
_obsidian_config: dict = {}

def _load_obsidian_config() -> dict:
    if _OBSIDIAN_CONFIG_PATH.exists():
        try:
            return json.loads(_OBSIDIAN_CONFIG_PATH.read_text())
        except Exception:
            pass
    return {}

_obsidian_config = _load_obsidian_config()

app.mount(
    "/static",
    StaticFiles(directory=Path(__file__).parent / "ui" / "static"),
    name="static",
)

# ── Global session state ──────────────────────────────────────────────────────
_session: SessionManager | None = None
_transcript_clients: list[WebSocket] = []
_notes_clients: list[WebSocket] = []


# ── WebSocket helpers ─────────────────────────────────────────────────────────

async def _broadcast_transcript(line: TranscriptLine) -> None:
    payload = json.dumps({
        "type": "transcript_line",
        "start": line.start,
        "end": line.end,
        "speaker_id": line.speaker_id,
        "speaker_label": line.speaker_label,
        "text": line.text,
    })
    dead = []
    for ws in _transcript_clients:
        try:
            await ws.send_text(payload)
        except Exception:
            dead.append(ws)
    for ws in dead:
        _transcript_clients.remove(ws)


async def _broadcast_notes() -> None:
    if _session is None:
        return
    notes = _session.get_notes()
    payload = json.dumps({"type": "notes_update", "notes": notes.model_dump()})
    dead = []
    for ws in _notes_clients:
        try:
            await ws.send_text(payload)
        except Exception:
            dead.append(ws)
    for ws in dead:
        _notes_clients.remove(ws)


# ── Player context routes ─────────────────────────────────────────────────────

@app.post("/player/context")
async def set_player_context(body: PlayerContextRequest):
    global _player_context
    _player_context = body.model_dump()
    _PLAYER_CONTEXT_PATH.write_text(json.dumps(_player_context, indent=2))
    return {"status": "saved"}


@app.get("/player/context")
async def get_player_context():
    return _player_context


# ── Obsidian routes ──────────────────────────────────────────────────────────

@app.get("/settings/obsidian")
async def get_obsidian_config():
    return _obsidian_config

@app.post("/settings/obsidian")
async def set_obsidian_config(payload: dict):
    global _obsidian_config
    vault_path = payload.get("vault_path", "").strip()
    subfolder = payload.get("subfolder", "D&D Sessions").strip()
    auto_export = payload.get("auto_export", True)

    # Validate vault path
    if vault_path:
        vault = Path(vault_path)
        if not vault.exists() or not vault.is_dir():
            return {"ok": False, "error": "Vault folder not found"}
        obsidian_dir = vault / ".obsidian"
        if not obsidian_dir.exists():
            return {"ok": False, "error": "Not an Obsidian vault (no .obsidian folder)"}

    _obsidian_config = {
        "vault_path": vault_path,
        "subfolder": subfolder,
        "auto_export": auto_export,
    }
    _OBSIDIAN_CONFIG_PATH.write_text(json.dumps(_obsidian_config, indent=2))
    return {"ok": True}

@app.post("/settings/obsidian/disconnect")
async def disconnect_obsidian():
    global _obsidian_config
    _obsidian_config = {}
    if _OBSIDIAN_CONFIG_PATH.exists():
        _OBSIDIAN_CONFIG_PATH.unlink()
    return {"ok": True}

@app.post("/settings/obsidian/browse")
async def browse_obsidian_vault():
    """Open a native folder picker and return the selected path."""
    import subprocess
    result = subprocess.run(
        ["osascript", "-e",
         'set theFolder to choose folder with prompt "Select your Obsidian vault folder"',
         "-e", 'POSIX path of theFolder'],
        capture_output=True, text=True, timeout=60,
    )
    if result.returncode != 0 or not result.stdout.strip():
        return {"ok": False, "path": ""}
    path = result.stdout.strip().rstrip("/")
    return {"ok": True, "path": path}


# ── Routes ────────────────────────────────────────────────────────────────────

@app.get("/", response_class=HTMLResponse)
async def index():
    html = (Path(__file__).parent / "ui" / "static" / "index.html").read_text()
    return HTMLResponse(content=html)


@app.get("/devices")
async def get_devices():
    return {"devices": list_input_devices()}


@app.post("/settings/mic-device")
async def set_mic_device(payload: dict):
    """Set the microphone device for dual-source capture (local player's voice)."""
    global _mic_device
    device_name = payload.get("device", "").strip() or None
    _mic_device = device_name
    if _session is not None:
        _session.set_mic_device(device_name)
    return {"ok": True, "device": device_name}


@app.get("/session/transcript_lines")
async def get_transcript_lines(offset: int = 0, diar_version: int = -1):
    """
    Return transcript lines from offset onward.
    If diar_version differs from current, also return full=True so the
    frontend knows to re-render all lines with updated speaker labels.
    """
    if _session is None:
        return {"lines": [], "total": 0, "diar_version": 0, "full_refresh": False}

    all_lines = _session.get_transcript()
    current_version = _session.diar_version
    full_refresh = diar_version != -1 and diar_version != current_version

    # On full refresh send everything; otherwise just new lines
    lines_to_send = all_lines if full_refresh else all_lines[offset:]

    return {
        "lines": [
            {
                "index": i if full_refresh else offset + i,
                "start": l.start,
                "end": l.end,
                "speaker_id": l.speaker_id,
                "speaker_label": l.speaker_label,
                "text": l.text,
            }
            for i, l in enumerate(lines_to_send)
        ],
        "total": len(all_lines),
        "diar_version": current_version,
        "full_refresh": full_refresh,
    }


@app.get("/session/notes")
async def get_current_notes():
    """Return the current session notes (for polling)."""
    if _session is None:
        return {"notes": None}
    return {"notes": _session.get_notes().model_dump()}


@app.get("/session/export/transcript")
async def export_transcript():
    """Download the current transcript as a markdown file."""
    if _session is None:
        return PlainTextResponse("No active session", status_code=404)
    transcript = _session.get_transcript()
    if not transcript:
        return PlainTextResponse("No transcript data yet", status_code=404)
    from src.session.storage import _write_transcript_str
    content = _write_transcript_str(transcript)
    return PlainTextResponse(
        content=content,
        media_type="application/octet-stream",
        headers={"Content-Disposition": "attachment; filename=transcript.txt"},
    )


@app.get("/session/export/notes")
async def export_notes():
    """Download the current session notes as a markdown file."""
    if _session is None:
        return PlainTextResponse("No active session", status_code=404)
    notes = _session.get_notes()
    if not notes.summary and not notes.npcs:
        return PlainTextResponse("No notes generated yet", status_code=404)
    from src.session.storage import _write_notes_str
    content = _write_notes_str(notes)
    return PlainTextResponse(
        content=content,
        media_type="application/octet-stream",
        headers={"Content-Disposition": "attachment; filename=notes.txt"},
    )


@app.get("/session/status")
async def session_status():
    if _session is None:
        return {"state": SessionState.IDLE, "elapsed": 0, "progress": ""}
    return {
        "state": _session.state,
        "elapsed": _session.elapsed_seconds,
        "progress": _session.progress_message,
    }


@app.get("/system/open-midi-setup")
async def open_midi_setup():
    """Open Audio MIDI Setup on macOS."""
    import subprocess
    subprocess.Popen(["open", "/Applications/Utilities/Audio MIDI Setup.app"])
    return {"ok": True}


@app.post("/settings/hf-token")
async def save_hf_token(payload: dict):
    """Save HuggingFace token to .env file."""
    import os
    token = payload.get("token", "").strip()
    if not token:
        return {"ok": False, "error": "No token provided"}

    env_path = Path(".env")
    lines = []
    if env_path.exists():
        lines = env_path.read_text().splitlines()

    # Replace or add the token line
    found = False
    for i, line in enumerate(lines):
        if line.startswith("HUGGINGFACE_TOKEN"):
            lines[i] = f"HUGGINGFACE_TOKEN={token}"
            found = True
            break
    if not found:
        lines.append(f"HUGGINGFACE_TOKEN={token}")

    env_path.write_text("\n".join(lines) + "\n")
    os.environ["HUGGINGFACE_TOKEN"] = token
    return {"ok": True}


@app.post("/session/start")
async def session_start(body: StartSessionRequest = StartSessionRequest()):
    global _session
    if _session and _session.state == SessionState.RUNNING:
        return {"error": "Session already running"}

    _session = SessionManager(CONFIG, player_context=_player_context or None)

    # Apply saved mic device so dual capture works from the start
    if _mic_device:
        _session.set_mic_device(_mic_device)

    async def on_transcript(line: TranscriptLine):
        await _broadcast_transcript(line)

    async def on_notes():
        await _broadcast_notes()

    _session.on_transcript_line(on_transcript)
    _session.on_notes_update(on_notes)

    await _session.start(session_name=body.session_name)
    return {"status": "started", "state": _session.state}


@app.post("/session/stop")
async def session_stop():
    global _session
    if _session is None or _session.state != SessionState.RUNNING:
        return {"error": "No active session"}
    await _session.stop()
    return {"status": "processing"}  # post-session diarization now running in background


@app.post("/session/rename_speaker")
async def rename_speaker(body: RenameSpeakerRequest):
    if _session is None:
        return {"error": "No active session"}
    _session.rename_speaker(body.speaker_id, body.label)
    return {"status": "ok"}


@app.get("/sessions")
async def list_sessions():
    output_dir = Path(CONFIG["output"]["directory"])
    if not output_dir.exists():
        return {"sessions": []}
    sessions = []
    for d in sorted(output_dir.iterdir(), reverse=True):
        if d.is_dir():
            notes_file = d / "notes.md"
            sessions.append({
                "id": d.name,
                "name": d.name,
                "has_notes": notes_file.exists(),
            })
    return {"sessions": sessions}


@app.get("/sessions/{session_id}/notes")
async def get_session_notes(session_id: str):
    notes_path = Path(CONFIG["output"]["directory"]) / session_id / "notes.md"
    if not notes_path.exists():
        return {"error": "Session not found"}
    return {"notes": notes_path.read_text()}


@app.get("/sessions/{session_id}/transcript")
async def get_session_transcript(session_id: str):
    path = Path(CONFIG["output"]["directory"]) / session_id / "transcript.md"
    if not path.exists():
        return {"error": "Transcript not found"}
    return {"transcript": path.read_text()}


@app.delete("/sessions/{session_id}")
async def delete_session(session_id: str):
    """Delete a saved session directory."""
    import shutil
    session_dir = Path(CONFIG["output"]["directory"]) / session_id
    if not session_dir.exists() or not session_dir.is_dir():
        return {"error": "Session not found"}
    # Safety: only delete within the output directory
    output_dir = Path(CONFIG["output"]["directory"]).resolve()
    if not session_dir.resolve().is_relative_to(output_dir):
        return {"error": "Invalid session path"}
    shutil.rmtree(session_dir)
    return {"ok": True}


# ── WebSockets ────────────────────────────────────────────────────────────────

@app.websocket("/ws/transcript")
async def ws_transcript(websocket: WebSocket):
    await websocket.accept()
    _transcript_clients.append(websocket)
    # Send existing transcript on connect
    if _session:
        for line in _session.get_transcript():
            await _broadcast_transcript(line)
    try:
        while True:
            await websocket.receive_text()  # keep alive, handle pings
    except WebSocketDisconnect:
        if websocket in _transcript_clients:
            _transcript_clients.remove(websocket)


@app.websocket("/ws/notes")
async def ws_notes(websocket: WebSocket):
    await websocket.accept()
    _notes_clients.append(websocket)
    # Send current notes on connect
    if _session:
        notes = _session.get_notes()
        await websocket.send_text(
            json.dumps({"type": "notes_update", "notes": notes.model_dump()})
        )
    try:
        while True:
            await websocket.receive_text()
    except WebSocketDisconnect:
        if websocket in _notes_clients:
            _notes_clients.remove(websocket)
