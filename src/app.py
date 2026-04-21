"""FastAPI application — REST API + WebSocket server."""

from __future__ import annotations

import json
import logging
from pathlib import Path
from typing import Any, Optional

import yaml
from fastapi import FastAPI, HTTPException
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import HTMLResponse, PlainTextResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel

from src import cloud_config
from src.audio.devices import list_input_devices
from src.campaign.models import Campaign
from src.campaign.storage import CampaignStore, save_campaign, slugify
from src.session.manager import SessionManager, SessionState
from src.transcription import TranscriptLine

# Load cloud API keys from Application Support/.env on startup
cloud_config.load_keys()

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

# ── Load config ───────────────────────────────────────────────────────────────
_CONFIG_PATH = Path(__file__).parent.parent / "config.yaml"
with open(_CONFIG_PATH) as f:
    CONFIG: dict = yaml.safe_load(f)

# ── App setup ─────────────────────────────────────────────────────────────────
app = FastAPI(title="Passive Perception")
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_methods=["*"],
    allow_headers=["*"],
)

app.mount(
    "/static",
    StaticFiles(directory=Path(__file__).parent / "ui" / "static"),
    name="static",
)

# ── Request models ────────────────────────────────────────────────────────────
class StartSessionRequest(BaseModel):
    session_name: Optional[str] = None


class RenameSpeakerRequest(BaseModel):
    speaker_id: str
    label: str


class ApiKeysRequest(BaseModel):
    deepgram: Optional[str] = None
    gemini: Optional[str] = None


class PreBriefRequest(BaseModel):
    brief: str = ""


class FinalizeRequest(BaseModel):
    labels: Optional[dict[str, str]] = None
    skip: bool = False


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

# ── Global session state ──────────────────────────────────────────────────────
_session: SessionManager | None = None
_mic_device: str | None = None


# ── API key routes ───────────────────────────────────────────────────────────

@app.get("/settings/api-keys")
async def get_api_key_status():
    """Returns boolean flags per provider — never returns the actual key."""
    return cloud_config.status()


@app.post("/settings/api-keys")
async def save_api_keys(body: ApiKeysRequest):
    cloud_config.save_keys(deepgram=body.deepgram, gemini=body.gemini)
    return {"ok": True, "status": cloud_config.status()}


# ── Campaign routes ──────────────────────────────────────────────────────────

@app.get("/campaigns")
async def list_campaigns():
    return {"campaigns": CampaignStore.list(), "active": _active_campaign_id()}


@app.get("/campaigns/active")
async def get_active_campaign():
    c = CampaignStore.active()
    return {"campaign": c.model_dump() if c else None}


@app.post("/campaigns/active")
async def set_active_campaign(payload: dict):
    campaign_id = (payload.get("id") or "").strip()
    if not campaign_id:
        CampaignStore.clear_active()
        return {"ok": True, "active": None}
    if not CampaignStore.load(campaign_id):
        raise HTTPException(404, f"Campaign '{campaign_id}' not found")
    CampaignStore.set_active(campaign_id)
    return {"ok": True, "active": campaign_id}


@app.get("/campaigns/{campaign_id}")
async def get_campaign(campaign_id: str):
    c = CampaignStore.load(campaign_id)
    if not c:
        raise HTTPException(404, f"Campaign '{campaign_id}' not found")
    return c.model_dump()


@app.post("/campaigns")
async def upsert_campaign(payload: dict):
    """Create or update a campaign. Body is the full Campaign shape."""
    if "id" not in payload or not payload["id"]:
        payload["id"] = slugify(payload.get("name", "untitled"))
    try:
        campaign = Campaign.model_validate(payload)
    except Exception as exc:
        raise HTTPException(400, f"Invalid campaign: {exc}")
    CampaignStore.save(campaign)
    return {"ok": True, "campaign": campaign.model_dump()}


@app.delete("/campaigns/{campaign_id}")
async def delete_campaign(campaign_id: str):
    if not CampaignStore.delete(campaign_id):
        raise HTTPException(404, f"Campaign '{campaign_id}' not found")
    return {"ok": True}


def _active_campaign_id() -> Optional[str]:
    c = CampaignStore.active()
    return c.id if c else None


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

    if vault_path:
        vault = Path(vault_path).expanduser()
        if not vault.exists() or not vault.is_dir():
            return {"ok": False, "error": "Vault folder not found"}
        if not (vault / ".obsidian").exists():
            return {"ok": False, "error": "Not an Obsidian vault (no .obsidian folder)"}
        vault_path = str(vault)

    _obsidian_config = {"vault_path": vault_path, "subfolder": subfolder, "auto_export": auto_export}
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


# ── Root + device routes ──────────────────────────────────────────────────────

@app.get("/", response_class=HTMLResponse)
async def index():
    html = (Path(__file__).parent / "ui" / "static" / "index.html").read_text()
    return HTMLResponse(content=html)


@app.get("/devices")
async def get_devices():
    return {"devices": list_input_devices()}


@app.post("/settings/mic-device")
async def set_mic_device(payload: dict):
    global _mic_device
    device_name = payload.get("device", "").strip() or None
    _mic_device = device_name
    if _session is not None:
        _session.set_mic_device(device_name)
    return {"ok": True, "device": device_name}


# ── Session polling routes ────────────────────────────────────────────────────

@app.get("/session/transcript_lines")
async def get_transcript_lines(offset: int = 0, diar_version: int = -1):
    if _session is None:
        return {"lines": [], "total": 0, "diar_version": 0, "full_refresh": False}

    all_lines = _session.get_transcript()
    current_version = _session.diar_version
    full_refresh = diar_version != -1 and diar_version != current_version
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
    if _session is None:
        return {"notes": None}
    return {"notes": _session.get_notes().model_dump()}


@app.get("/session/export/transcript")
async def export_transcript():
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
        return {"state": SessionState.IDLE, "elapsed": 0, "progress": "", "session_id": None}
    return {
        "state": _session.state,
        "elapsed": _session.elapsed_seconds,
        "progress": _session.progress_message,
        "session_id": _session.session_id,
    }


# ── Pre-session brief (on the active campaign) ────────────────────────────────

@app.post("/session/pre-brief")
async def save_pre_brief(body: PreBriefRequest):
    campaign = CampaignStore.active()
    if campaign is None:
        raise HTTPException(400, "No active campaign")
    campaign.pending_session_brief = body.brief or ""
    save_campaign(campaign)
    return {"ok": True, "brief": campaign.pending_session_brief}


@app.get("/session/pre-brief")
async def get_pre_brief():
    campaign = CampaignStore.active()
    return {"brief": campaign.pending_session_brief if campaign else ""}


# ── Pass 1 results + finalize + resume ───────────────────────────────────────

@app.get("/session/pass1")
async def get_pass1():
    """Return the Pass 1 result (speaker summaries + classification tags) + canonical transcript."""
    if _session is None:
        raise HTTPException(404, "No session")
    pass1 = _session.get_pass1_result()
    if pass1 is None:
        return {"ready": False}
    transcript = _session.get_transcript()
    return {
        "ready": True,
        "state": _session.state,
        "pass1": pass1.model_dump(),
        "transcript": [
            {
                "index": i,
                "start": ln.start,
                "end": ln.end,
                "speaker_id": ln.speaker_id,
                "speaker_label": ln.speaker_label,
                "text": ln.text,
            }
            for i, ln in enumerate(transcript)
        ],
    }


@app.post("/session/transcript/reassign")
async def reassign_transcript_line(payload: dict):
    """Move a single line to a different speaker (used during labeling to fix diarization mis-splits)."""
    if _session is None:
        raise HTTPException(404, "No session")
    if _session.state != SessionState.AWAITING_LABELS:
        raise HTTPException(409, f"Can only reassign lines in AWAITING_LABELS state (current: {_session.state})")
    try:
        line_index = int(payload.get("line_index"))
        new_speaker_id = str(payload.get("speaker_id", "")).strip()
    except (TypeError, ValueError):
        raise HTTPException(400, "line_index (int) and speaker_id (str) required")
    try:
        _session.reassign_line(line_index, new_speaker_id)
    except ValueError as exc:
        raise HTTPException(400, str(exc))
    return {"ok": True}


@app.post("/session/labels")
async def set_labels(payload: dict):
    """Apply speaker labels without triggering Pass 2 (lets the UI save progress as the user types)."""
    if _session is None:
        raise HTTPException(404, "No session")
    labels = {k: str(v) for k, v in (payload.get("labels") or {}).items() if v}
    _session.apply_labels(labels)
    return {"ok": True}


@app.post("/session/finalize")
async def finalize_session(body: FinalizeRequest):
    if _session is None:
        raise HTTPException(404, "No session")
    if _session.state != SessionState.AWAITING_LABELS:
        raise HTTPException(409, f"Session is in {_session.state}; cannot finalize")
    try:
        await _session.finalize(labels=body.labels, skip=body.skip)
    except RuntimeError as exc:
        raise HTTPException(409, str(exc))
    return {"ok": True, "state": _session.state}


@app.get("/session/resumable")
async def list_resumable():
    from src.session.storage import list_resumable_sessions
    output_dir = Path(CONFIG["output"]["directory"])
    dirs = list_resumable_sessions(output_dir)
    return {"sessions": [{"id": p.name, "name": p.name} for p in dirs]}


@app.post("/session/resume/{session_id}")
async def resume_session(session_id: str):
    global _session
    if _session and _session.state not in (SessionState.IDLE,):
        raise HTTPException(409, f"Another session is active ({_session.state})")

    output_dir = Path(CONFIG["output"]["directory"])
    session_dir = output_dir / session_id
    if not session_dir.is_dir():
        raise HTTPException(404, "Session not found")

    campaign = CampaignStore.active()
    try:
        _session = SessionManager.resume_from_pass1(CONFIG, campaign, session_dir)
    except Exception as exc:
        raise HTTPException(400, f"Failed to resume: {exc}")

    async def on_notes():
        pass  # no websocket broadcast any more — UI polls

    _session.on_notes_update(on_notes)
    return {"ok": True, "state": _session.state, "session_id": _session.session_id}


@app.get("/system/open-midi-setup")
async def open_midi_setup():
    import subprocess
    subprocess.Popen(["open", "/Applications/Utilities/Audio MIDI Setup.app"])
    return {"ok": True}


# ── Session lifecycle ────────────────────────────────────────────────────────

@app.post("/session/start")
async def session_start(body: StartSessionRequest = StartSessionRequest()):
    global _session
    if _session and _session.state == SessionState.RUNNING:
        return {"error": "Session already running"}

    # Preflight — both API keys required
    missing = [k for k, ok in cloud_config.status().items() if not ok]
    if missing:
        return {"error": f"Missing API keys: {', '.join(missing)}. Add them in Settings → API Keys."}

    campaign = CampaignStore.active()
    if campaign is None:
        return {"error": "No active campaign. Create or select one in Settings → Campaigns."}

    _session = SessionManager(CONFIG, campaign=campaign)

    if _mic_device:
        _session.set_mic_device(_mic_device)

    await _session.start(session_name=body.session_name)
    return {"status": "started", "state": _session.state}


@app.post("/session/stop")
async def session_stop():
    global _session
    if _session is None or _session.state != SessionState.RUNNING:
        return {"error": "No active session"}
    await _session.stop()
    return {"status": "processing"}


@app.post("/session/rename_speaker")
async def rename_speaker(body: RenameSpeakerRequest):
    if _session is None:
        return {"error": "No active session"}
    _session.rename_speaker(body.speaker_id, body.label)
    return {"status": "ok"}


# ── Saved session archive ─────────────────────────────────────────────────────

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
    import shutil
    session_dir = Path(CONFIG["output"]["directory"]) / session_id
    if not session_dir.exists() or not session_dir.is_dir():
        return {"error": "Session not found"}
    output_dir = Path(CONFIG["output"]["directory"]).resolve()
    if not session_dir.resolve().is_relative_to(output_dir):
        return {"error": "Invalid session path"}
    shutil.rmtree(session_dir)
    return {"ok": True}
