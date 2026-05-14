"""Locate and probe the bundled pp-system-audio Swift helper."""

from __future__ import annotations

import logging
import os
import subprocess
from pathlib import Path

logger = logging.getLogger(__name__)


def locate_system_audio_helper() -> Path:
    """
    Return the absolute path to the pp-system-audio binary.

    Searches two layouts:
      - Bundle: <PassivePerception.app>/Contents/MacOS/pp-system-audio
      - Dev:    <repo>/native/macos/pp-system-audio/.build/release/pp-system-audio

    Raises FileNotFoundError if neither is found.
    """
    here = Path(__file__).resolve()
    seen: set[Path] = set()
    for parent in (here.parent, *here.parents):
        for candidate in (
            parent / "MacOS" / "pp-system-audio",
            parent / "native" / "macos" / "pp-system-audio" / ".build" / "release" / "pp-system-audio",
        ):
            if candidate in seen:
                continue
            seen.add(candidate)
            if candidate.is_file() and os.access(candidate, os.X_OK):
                return candidate
    raise FileNotFoundError(
        "pp-system-audio helper not found. If running from source, build it with: "
        "(cd native/macos/pp-system-audio && swift build -c release --arch arm64)"
    )


def check_os_supported() -> bool:
    """
    Probe the helper for OS support (macOS >= 14.2). Side-effect free —
    does NOT trigger the TCC permission prompt.

    Returns False if the helper is missing, can't run, or reports < 14.2.
    """
    try:
        helper = locate_system_audio_helper()
    except FileNotFoundError:
        return False
    try:
        result = subprocess.run(
            [str(helper), "--check-os"],
            timeout=2.0,
            capture_output=True,
        )
    except (subprocess.SubprocessError, OSError) as exc:
        logger.warning("pp-system-audio --check-os failed: %s", exc)
        return False
    return result.returncode == 0
