"""
Campaign storage — JSON files per campaign on disk.

Layout:
  ~/Documents/Obsidian/PassivePerception/campaigns/<slug>.json    (if Obsidian configured)
  OR
  <app config dir>/campaigns/<slug>.json                          (fallback)

A single "active campaign" pointer is stored alongside so the session flow
always knows which roster to use without the UI having to pass it around.
"""

from __future__ import annotations

import json
import logging
import re
from pathlib import Path
from typing import Optional

from pydantic import ValidationError

from src.campaign.models import (
    Campaign,
    CampaignCharacter,
    CampaignLocation,
    CampaignNPC,
    CampaignPlotThread,
    CampaignState,
)
from src.notes.models import SessionNotes
from src.platform_utils import app_support_dir, obsidian_vault_candidates

logger = logging.getLogger(__name__)


def _campaigns_dir() -> Path:
    """
    Prefer an Obsidian-vault location (so campaigns sync with the user's
    other notes via iCloud/OneDrive/Obsidian Sync). Falls back to the app
    support dir when no vault is present.

    On Windows, the vault may live under OneDrive-redirected Documents —
    `obsidian_vault_candidates()` probes both paths.
    """
    for vault_root in obsidian_vault_candidates():
        pp_dir = vault_root / "PassivePerception" / "campaigns"
        pp_dir.mkdir(parents=True, exist_ok=True)
        return pp_dir
    fallback = app_support_dir() / "campaigns"
    fallback.mkdir(parents=True, exist_ok=True)
    return fallback


def _active_pointer_path() -> Path:
    return _campaigns_dir() / ".active"


def slugify(name: str) -> str:
    slug = re.sub(r"[^a-z0-9]+", "-", (name or "").lower()).strip("-")
    return slug or "untitled"


# ── Load / Save / List ───────────────────────────────────────────────────────


def _path_for(campaign_id: str) -> Path:
    return _campaigns_dir() / f"{slugify(campaign_id)}.json"


def load_campaign(campaign_id: str) -> Optional[Campaign]:
    path = _path_for(campaign_id)
    if not path.exists():
        return None
    try:
        return Campaign.model_validate_json(path.read_text())
    except (ValidationError, json.JSONDecodeError) as exc:
        logger.error("Campaign %s is corrupt: %s", campaign_id, exc)
        return None


def save_campaign(campaign: Campaign) -> Path:
    path = _path_for(campaign.id)
    path.write_text(campaign.model_dump_json(indent=2))
    return path


def list_campaigns() -> list[dict]:
    """Return [{id, name, system, session_count}] for the UI picker."""
    results = []
    for p in sorted(_campaigns_dir().glob("*.json")):
        try:
            data = json.loads(p.read_text())
            results.append({
                "id": data.get("id", p.stem),
                "name": data.get("name", p.stem),
                "system": data.get("system", ""),
                "session_count": len(data.get("session_ids", [])),
            })
        except Exception:
            continue
    return results


def active_campaign() -> Optional[Campaign]:
    """Return the currently-active campaign, or None if none is selected."""
    pointer = _active_pointer_path()
    if not pointer.exists():
        return None
    campaign_id = pointer.read_text().strip()
    if not campaign_id:
        return None
    return load_campaign(campaign_id)


def set_active_campaign(campaign_id: str) -> None:
    _active_pointer_path().write_text(campaign_id)


def clear_active_campaign() -> None:
    pointer = _active_pointer_path()
    if pointer.exists():
        pointer.unlink()


def delete_campaign(campaign_id: str) -> bool:
    path = _path_for(campaign_id)
    if not path.exists():
        return False
    path.unlink()
    # If the deleted campaign was active, clear the pointer.
    pointer = _active_pointer_path()
    if pointer.exists() and pointer.read_text().strip() == slugify(campaign_id):
        clear_active_campaign()
    return True


# ── Merging session output back into the campaign ────────────────────────────


def merge_session_into_campaign(
    campaign: Campaign,
    notes: SessionNotes,
    session_id: str,
) -> Campaign:
    """
    Merge a session's extracted notes into the persistent campaign.

    New NPCs / locations are appended. Known ones have their last_session
    stamp refreshed. Plot points become new plot threads (status=active).
    Open questions from the session are added as unresolved_hooks on the
    campaign state. The state summary becomes the session summary.
    """
    known_npc_names = {n.name.lower() for n in campaign.npcs}
    for npc in notes.npcs:
        key = (npc.name or "").strip().lower()
        if not key:
            continue
        if key in known_npc_names:
            for existing in campaign.npcs:
                if existing.name.lower() == key:
                    existing.last_session = session_id
                    if not existing.description and npc.description:
                        existing.description = npc.description
                    if npc.relationship and npc.relationship != "unknown":
                        existing.relationship = npc.relationship
                    if npc.notes:
                        existing.notes = (existing.notes + "\n" + npc.notes).strip() if existing.notes else npc.notes
                    break
        else:
            campaign.npcs.append(
                CampaignNPC(
                    name=npc.name,
                    description=npc.description,
                    relationship=npc.relationship or "unknown",
                    notes=npc.notes,
                    first_session=session_id,
                    last_session=session_id,
                )
            )
            known_npc_names.add(key)

    known_loc_names = {loc.name.lower() for loc in campaign.locations}
    for loc in notes.locations:
        key = (loc.name or "").strip().lower()
        if not key or key in known_loc_names:
            continue
        campaign.locations.append(
            CampaignLocation(
                name=loc.name,
                description=loc.description,
                significance=loc.significance,
            )
        )
        known_loc_names.add(key)

    existing_plot_summaries = {pt.summary.lower().strip() for pt in campaign.plot_threads}
    for plot in notes.plot_points:
        key = plot.summary.lower().strip()
        if not key or key in existing_plot_summaries:
            continue
        campaign.plot_threads.append(
            CampaignPlotThread(
                summary=plot.summary,
                status="active",
                related_npcs=list(plot.npcs_involved),
                opened_session=session_id,
            )
        )
        existing_plot_summaries.add(key)

    # Update running state
    campaign.state = CampaignState(
        summary=notes.summary or campaign.state.summary,
        current_location=campaign.state.current_location,
        party_status=campaign.state.party_status,
        immediate_next_steps=campaign.state.immediate_next_steps,
        unresolved_hooks=list(dict.fromkeys(
            [*campaign.state.unresolved_hooks, *notes.open_questions]
        ))[:20],  # cap so this doesn't grow forever
    )

    if session_id and session_id not in campaign.session_ids:
        campaign.session_ids.append(session_id)

    # The brief was consumed by this session's notes pass — clear it so the
    # next session doesn't accidentally re-use stale setup.
    campaign.pending_session_brief = ""

    return campaign


# ── Convenience: the store facade used by app.py ─────────────────────────────


class CampaignStore:
    """Thin wrapper to keep app.py tidy."""

    @staticmethod
    def list() -> list[dict]:
        return list_campaigns()

    @staticmethod
    def load(campaign_id: str) -> Optional[Campaign]:
        return load_campaign(campaign_id)

    @staticmethod
    def save(campaign: Campaign) -> Path:
        return save_campaign(campaign)

    @staticmethod
    def active() -> Optional[Campaign]:
        return active_campaign()

    @staticmethod
    def set_active(campaign_id: str) -> None:
        set_active_campaign(campaign_id)

    @staticmethod
    def clear_active() -> None:
        clear_active_campaign()

    @staticmethod
    def delete(campaign_id: str) -> bool:
        return delete_campaign(campaign_id)
