"""
Pydantic models for a persistent campaign roster.

A campaign is a single ongoing story — the user's character, their party,
the NPCs and locations encountered, and the plot threads in play. It is
the context that makes session notes intelligible across weeks: without it,
every session starts from zero and the LLM has to re-discover who everyone is.

Used two ways:
  1. Deepgram keyterm biasing — names from the roster are fed to the
     transcription API so fantasy spellings come out correctly.
  2. Gemini system prompt — the full roster + current state + player
     perspective is prepended so extraction + summarization are continuous
     across sessions and geared toward what matters to the user.
"""

from __future__ import annotations

from pydantic import BaseModel, Field


class CampaignCharacter(BaseModel):
    """A character — the player themselves or a party member."""
    name: str
    role: str = "party"  # "player" (the user), "party" (other PCs), "ally" (GM-run NPC in party)
    race: str = ""
    char_class: str = ""
    subclass: str = ""
    multi_class: str = ""
    multi_subclass: str = ""
    pronouns: str = ""
    description: str = ""      # appearance / personality
    backstory: str = ""        # personal history that matters to them
    goals: str = ""             # what this character is driving toward
    notes: str = ""             # anything else worth carrying forward


class CampaignNPC(BaseModel):
    """A non-player character the party has encountered."""
    name: str
    aliases: list[str] = Field(default_factory=list)   # name variants — useful for both Deepgram keyterm biasing and LLM de-duplication
    description: str = ""
    relationship: str = "unknown"   # ally, enemy, neutral, rival, unknown
    faction: str = ""
    location: str = ""              # where they're usually found
    notes: str = ""                 # motivations, secrets, hooks
    first_session: str = ""
    last_session: str = ""


class CampaignLocation(BaseModel):
    """A named place in the world."""
    name: str
    aliases: list[str] = Field(default_factory=list)
    region: str = ""
    description: str = ""
    significance: str = ""
    notes: str = ""


class CampaignPlotThread(BaseModel):
    """A storyline in play — kept across sessions so continuity holds."""
    summary: str
    status: str = "active"          # active | resolved | dormant
    related_npcs: list[str] = Field(default_factory=list)
    related_locations: list[str] = Field(default_factory=list)
    opened_session: str = ""
    closed_session: str = ""


class CampaignState(BaseModel):
    """Where things stand as of the most recent session."""
    summary: str = ""                    # narrative recap — "last time on..."
    current_location: str = ""
    party_status: str = ""               # HP, resources, conditions worth remembering
    immediate_next_steps: str = ""        # what the party had planned to do next
    unresolved_hooks: list[str] = Field(default_factory=list)


class Campaign(BaseModel):
    """The full persistent campaign."""
    id: str                               # filesystem slug — also the filename stem
    name: str                             # human display name
    system: str = "D&D 5e"                # game system — flavor for the LLM
    setting: str = ""                     # world/setting name + blurb

    player: CampaignCharacter             # THE USER — notes are geared toward this character
    perspective_notes: str = ""           # free-form: "emphasize clues related to X", "I care about faction politics", etc.

    party: list[CampaignCharacter] = Field(default_factory=list)    # other PCs and allied NPCs in the party
    npcs: list[CampaignNPC] = Field(default_factory=list)
    locations: list[CampaignLocation] = Field(default_factory=list)
    plot_threads: list[CampaignPlotThread] = Field(default_factory=list)

    state: CampaignState = Field(default_factory=CampaignState)

    pending_session_brief: str = ""   # filled in before pressing Record; cleared on successful Pass 2 merge

    session_ids: list[str] = Field(default_factory=list)   # references to saved sessions, newest last

    # ── Convenience for the transcription + LLM layers ───────────────────────

    def keyterms(self) -> list[str]:
        """
        Flat list of proper-noun strings to bias Deepgram transcription.
        Every name and alias across PCs, NPCs, and locations.
        Used for Deepgram Nova-3's `keyterm` parameter.
        """
        terms: set[str] = set()

        def add(s: str) -> None:
            s = (s or "").strip()
            if s:
                terms.add(s)

        add(self.player.name)
        for c in self.party:
            add(c.name)
        for n in self.npcs:
            add(n.name)
            for a in n.aliases:
                add(a)
        for loc in self.locations:
            add(loc.name)
            for a in loc.aliases:
                add(a)

        return sorted(terms)
