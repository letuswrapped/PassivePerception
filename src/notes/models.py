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
