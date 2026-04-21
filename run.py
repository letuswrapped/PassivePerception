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
        print("       Install with: brew install blackhole-2ch")

    # Cloud API keys (Deepgram + Gemini) — loaded from Application Support/.env
    from src import cloud_config
    cloud_config.load_keys()
    status = cloud_config.status()
    for provider, ok in status.items():
        if not ok:
            print(f"[warn] {provider.capitalize()} API key not set — configure it in Settings → API Keys.")


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
        if _session is None or _session.state == "idle":
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
    signal.signal(signal.SIGINT,  lambda *_: sys.exit(0))  # Ctrl+C / force quit

    # Start server in background thread
    server_thread = threading.Thread(target=start_server, daemon=True)
    server_thread.start()

    print("[info] Waiting for server...")
    if not wait_for_server():
        print("[error] Server failed to start. Check for port conflicts on 8000.")
        sys.exit(1)

    print(f"[info] Server ready at {URL}")

    # Unified titlebar (Safari / Xcode look) — traffic lights float over the
    # content, no gray separator bar. pywebview 6.x applies the required
    # NSWindow flags (`titlebarAppearsTransparent`, `titleVisibility=hidden`,
    # `NSFullSizeContentViewWindowMask`) only on the `frameless=True` code
    # path — setting them after the window is laid out is a no-op because
    # WKWebView's content view has already been positioned below the titlebar.
    #
    # Frameless also hides the traffic lights by default, so we re-show them
    # after the window is displayed. `easy_drag=False` because the topbar CSS
    # already declares a `-webkit-app-region: drag` region — enabling easy_drag
    # would make the entire window draggable (confirmed-bad UX).
    window = webview.create_window(
        title="Passive Perception",
        url=URL,
        width=1280,
        height=800,
        min_size=(900, 600),
        resizable=True,
        text_select=True,
        frameless=True,
        easy_drag=False,
    )

    def _restore_traffic_lights():
        try:
            import AppKit
            from PyObjCTools import AppHelper

            def apply():
                nswin = window.native
                for btn in (
                    AppKit.NSWindowCloseButton,
                    AppKit.NSWindowMiniaturizeButton,
                    AppKit.NSWindowZoomButton,
                ):
                    nswin.standardWindowButton_(btn).setHidden_(False)

            AppHelper.callAfter(apply)
        except Exception as exc:
            print(f"[warn] Could not restore traffic lights: {exc}")

    window.events.shown += _restore_traffic_lights

    # ── Topbar-scoped window drag ─────────────────────────────────────────
    # WKWebView eats all mouse events before they hit the NSWindow, so
    # `setMovableByWindowBackground_` and `-webkit-app-region: drag` both
    # no-op. pywebview's `easy_drag=True` subclasses WKWebView's mouseDown
    # to drag the window, but unconditionally — the entire window becomes a
    # drag surface, which breaks text selection and feels wrong.
    #
    # We patch `WebKitHost.mouseDown_` / `mouseDragged_` so drag only engages
    # when the click *started* in the top 56px (the CSS topbar region). Uses
    # `NSWindow.performWindowDragWithEvent:` (10.11+) — AppKit's own drag
    # loop, so behavior matches native windows exactly. Single clicks (no
    # drag motion) still forward normally, so buttons in the topbar keep
    # working.
    from webview.platforms.cocoa import BrowserView
    _TOPBAR_HEIGHT = 56  # pixels; matches #topbar rendered height
    _orig_mouseDown    = BrowserView.WebKitHost.mouseDown_
    _orig_mouseDragged = BrowserView.WebKitHost.mouseDragged_

    def _patched_mouseDown(self, event):
        i = BrowserView.get_instance('webview', self)
        if i is not None and i.frameless:
            loc = event.locationInWindow()
            bounds = self.bounds()
            i._mousedown_in_topbar = (bounds.size.height - loc.y) <= _TOPBAR_HEIGHT
        _orig_mouseDown(self, event)

    def _patched_mouseDragged(self, event):
        i = BrowserView.get_instance('webview', self)
        if i is not None and getattr(i, '_mousedown_in_topbar', False):
            self.window().performWindowDragWithEvent_(event)
            return
        _orig_mouseDragged(self, event)

    BrowserView.WebKitHost.mouseDown_    = _patched_mouseDown
    BrowserView.WebKitHost.mouseDragged_ = _patched_mouseDragged

    def on_closing():
        """Save session before window closes."""
        print("[info] Window closing — saving session...")
        _emergency_save()
        return True  # allow close

    window.events.closing += on_closing
    webview.start(debug=False)


if __name__ == "__main__":
    main()
