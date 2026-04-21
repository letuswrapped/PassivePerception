"""Pydantic models for structured D&D session notes."""

from __future__ import annotations

from pydantic import BaseModel, Field


class NPC(BaseModel):
    name: str
    description: str = ""
    relationship: str = ""       # e.g. "enemy", "ally", "neutral", "unknown"
    first_seen: str = ""         # timestamp or session context
    last_seen: str = ""
    notes: str = ""              # anything that doesn't fit the other fields


class Location(BaseModel):
    name: str
    description: str = ""
    significance: str = ""       # why it matters to the story


class PlotPoint(BaseModel):
    summary: str
    npcs_involved: list[str] = Field(default_factory=list)
    context: str = ""            # broader story context


class SessionNotes(BaseModel):
    summary: str = ""
    npcs: list[NPC] = Field(default_factory=list)
    locations: list[Location] = Field(default_factory=list)
    plot_points: list[PlotPoint] = Field(default_factory=list)
    open_questions: list[str] = Field(default_factory=list)


# ── Pass 1 output schemas (speaker summaries + per-utterance classification) ──
#
# Pass 1 runs once post-session on the full diarized transcript. It produces
# human-readable speaker cards for the labeling UI plus a flat list of tags
# that mark each utterance as in_character or other (table-talk, rules
# lookups, off-topic chatter). Pass 2 uses the user-assigned labels + the
# in_character filter to produce the final SessionNotes.


class SpeakerSummary(BaseModel):
    speaker_id: str                                   # e.g. "SPEAKER_00"
    utterance_count: int = 0
    total_seconds: float = 0.0
    summary: str = ""                                  # one sentence, ≤160 chars
    role_guess: str = "unknown"                        # "DM" | "player" | "unknown"
    roster_guess: str = ""                             # best-match PC name from Campaign party/player ("" if unsure)
    sample_quote_indices: list[int] = Field(default_factory=list)   # transcript line indices


class UtteranceTag(BaseModel):
    index: int                                        # position in canonical transcript
    tag: str = "in_character"                          # "in_character" | "other"


class Pass1Result(BaseModel):
    speakers: list[SpeakerSummary] = Field(default_factory=list)
    tags: list[UtteranceTag] = Field(default_factory=list)
