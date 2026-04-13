#!/usr/bin/env python3
"""
Entry point — starts the FastAPI server in a background thread,
then opens Passive Perception in a native macOS app window via pywebview.
"""

import atexit
import os
import signal
import sys
import threading
import time
from pathlib import Path

import httpx
import uvicorn
import webview
from dotenv import load_dotenv

load_dotenv()

HOST = "127.0.0.1"
PORT = 8000
URL  = f"http://{HOST}:{PORT}"


def check_prerequisites() -> None:
    # BlackHole
    hal_dir = Path("/Library/Audio/Plug-Ins/HAL")
    if hal_dir.exists():
        blackhole = any("blackhole" in p.name.lower() for p in hal_dir.iterdir())
    else:
        blackhole = False
    if not blackhole:
        print("[warn] BlackHole audio driver not found.")
        print("       Run ./setup.sh or: brew install blackhole-2ch")

    # HuggingFace token
    if not os.environ.get("HUGGINGFACE_TOKEN", "").strip():
        print("[warn] HUGGINGFACE_TOKEN not set — speaker diarization will be disabled.")
        print("       Add it to a .env file in this directory.")


def start_server() -> None:
    """Run FastAPI/uvicorn in a background daemon thread."""
    uvicorn.run(
        "src.app:app",
        host=HOST,
        port=PORT,
        reload=False,
        log_level="warning",
    )


def wait_for_server(timeout: int = 15) -> bool:
    """Poll until the server is accepting connections."""
    deadline = time.time() + timeout
    while time.time() < deadline:
        try:
            httpx.get(URL, timeout=1)
            return True
        except Exception:
            time.sleep(0.2)
    return False


def _emergency_save() -> None:
    """Best-effort save of any in-progress session data on unexpected exit."""
    try:
        from src.app import _session
        if _session is None or _session.state.value == "idle":
            return

        transcript = _session.get_transcript()
        if not transcript:
            return

        from src.session.storage import save_session
        session_dir = _session._session_dir
        if session_dir is None:
            session_dir = Path("sessions") / "emergency_save"
        session_dir.mkdir(parents=True, exist_ok=True)

        notes = _session.get_notes()
        save_session(
            session_dir=session_dir,
            transcript=transcript,
            notes=notes,
            auto_delete_audio=False,  # keep audio for recovery
        )
        print(f"\n[emergency] Session saved to {session_dir}")
    except Exception as exc:
        print(f"\n[emergency] Failed to save session: {exc}")


def main() -> None:
    print("\n  Passive Perception — D&D Session Scribe")
    check_prerequisites()

    # Register emergency save for unexpected exits
    atexit.register(_emergency_save)
    signal.signal(signal.SIGTERM, lambda *_: sys.exit(0))  # triggers atexit

    # Start server in background thread
    server_thread = threading.Thread(target=start_server, daemon=True)
    server_thread.start()

    print("[info] Waiting for server...")
    if not wait_for_server():
        print("[error] Server failed to start. Check for port conflicts on 8000.")
        sys.exit(1)

    print(f"[info] Server ready at {URL}")

    # Open native app window
    window = webview.create_window(
        title="Passive Perception",
        url=URL,
        width=1280,
        height=800,
        min_size=(900, 600),
        resizable=True,
        text_select=True,
    )
    webview.start(debug=False)


if __name__ == "__main__":
    main()
