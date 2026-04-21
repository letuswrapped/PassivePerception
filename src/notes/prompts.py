"""
System prompt + message builder for the Gemini note-extraction pass.

Two things make these notes good:

  1. **Campaign context.** Known PCs, NPCs, locations, plot threads, and the
     state recap get injected into the system prompt. This is what keeps
     notes continuous across sessions and de-duplicates entities between
     runs (no more "Gorzav" and "Gorzaph" being two different NPCs).

  2. **Player perspective.** The user's own character, their goals, and
     their personal priorities bias the extraction. Things that matter to
     *this* player (clues about their backstory, interactions involving
     their character, factions they care about) get surfaced first and
     phrased in terms of "you/your character" rather than a neutral recap.
"""

from __future__ import annotations

from src.campaign.models import Campaign


_BASE_SYSTEM = """You are a personal D&D session scribe. You take a noisy session transcript and produce structured notes for a single player who is part of the party.

Crucial: these notes are written FOR that player, not for the DM or for posterity. Prioritize what matters to them:
- Moments their own character was directly involved in
- Clues connected to their character's backstory, goals, or factions
- NPCs who interacted with them (even briefly)
- Information the player now knows versus what the party at large knows
- Follow-ups their character would want to remember next session

The transcript will contain speech-recognition errors, crosstalk, and out-of-character chatter (rules lookups, side jokes, bathroom breaks). Ignore meta-talk entirely; extract only in-character events and story content.

Extract the following:

1. **Session Summary** — A 3-6 sentence narrative recap written in second person ("You and the party..."). Past tense. Focus on what changed this session.

2. **NPCs** — Named non-player characters that appeared. For each:
   - name: their name (use the campaign roster spelling if they appear there; otherwise use the most consistent spelling from the transcript)
   - description: brief physical/personality notes if given
   - relationship: ally, enemy, neutral, rival, unknown
   - first_seen, last_seen: brief context — e.g. "tavern negotiation"
   - notes: motivations, secrets, or hooks tied specifically to the player

3. **Locations** — Named places visited or referenced. For each:
   - name, description, significance

4. **Plot Points** — 3-7 significant story beats or revelations. For each:
   - summary (one sentence)
   - npcs_involved
   - context (why this matters, especially to the player)

5. **Open Questions** — Unresolved mysteries or decisions the player will need to come back to. Each one sentence, phrased as a question.

Rules:
- Do NOT invent details not supported by the transcript. Omit uncertain things.
- Consolidate name variants using the campaign roster when ambiguous.
- Output ONLY JSON matching the supplied schema. No prose, no markdown fences, no explanation.
- Every top-level field is required even if empty (use [] or "")."""


def _character_block(campaign: Campaign) -> str:
    p = campaign.player
    bits = [f"- Name: {p.name}"]
    if p.pronouns:
        bits.append(f"- Pronouns: {p.pronouns}")
    class_desc = " ".join(filter(None, [p.race, p.char_class, p.subclass])).strip()
    if class_desc:
        bits.append(f"- Character: {class_desc}")
    if p.multi_class:
        mc = p.multi_class + (f" ({p.multi_subclass})" if p.multi_subclass else "")
        bits.append(f"- Multiclass: {mc}")
    if p.description:
        bits.append(f"- Description: {p.description}")
    if p.backstory:
        bits.append(f"- Backstory: {p.backstory}")
    if p.goals:
        bits.append(f"- Goals: {p.goals}")
    if p.notes:
        bits.append(f"- Notes: {p.notes}")
    return "\n".join(bits)


def _party_block(campaign: Campaign) -> str:
    if not campaign.party:
        return ""
    lines = ["Party members (other PCs — do NOT confuse with NPCs):"]
    for c in campaign.party:
        desc = " ".join(filter(None, [c.race, c.char_class, c.subclass])).strip()
        tag = f" — {desc}" if desc else ""
        lines.append(f"  - {c.name}{tag}")
    return "\n".join(lines)


def _npc_roster(campaign: Campaign, max_entries: int = 60) -> str:
    if not campaign.npcs:
        return ""
    lines = ["Known NPCs (use these exact spellings; don't create duplicates):"]
    for n in campaign.npcs[:max_entries]:
        bits = [f"  - {n.name}"]
        extras = [s for s in (n.relationship, n.faction, n.description) if s]
        if extras:
            bits.append(" — " + ", ".join(extras))
        lines.append("".join(bits))
    if len(campaign.npcs) > max_entries:
        lines.append(f"  …and {len(campaign.npcs) - max_entries} more.")
    return "\n".join(lines)


def _location_roster(campaign: Campaign, max_entries: int = 40) -> str:
    if not campaign.locations:
        return ""
    lines = ["Known locations (use these exact spellings):"]
    for loc in campaign.locations[:max_entries]:
        extras = [s for s in (loc.region, loc.description) if s]
        tag = " — " + ", ".join(extras) if extras else ""
        lines.append(f"  - {loc.name}{tag}")
    if len(campaign.locations) > max_entries:
        lines.append(f"  …and {len(campaign.locations) - max_entries} more.")
    return "\n".join(lines)


def _plot_threads_block(campaign: Campaign) -> str:
    active = [pt for pt in campaign.plot_threads if pt.status == "active"]
    if not active:
        return ""
    lines = ["Active plot threads (carry these forward):"]
    for pt in active[:15]:
        lines.append(f"  - {pt.summary}")
    return "\n".join(lines)


def _state_block(campaign: Campaign) -> str:
    s = campaign.state
    if not any([s.summary, s.current_location, s.immediate_next_steps, s.unresolved_hooks]):
        return ""
    lines = ["Last time on this campaign:"]
    if s.summary:
        lines.append(f"  - Recap: {s.summary}")
    if s.current_location:
        lines.append(f"  - Last location: {s.current_location}")
    if s.immediate_next_steps:
        lines.append(f"  - Planned next: {s.immediate_next_steps}")
    if s.unresolved_hooks:
        lines.append("  - Open hooks:")
        for h in s.unresolved_hooks[:10]:
            lines.append(f"      • {h}")
    return "\n".join(lines)


def _brief_block(campaign: Campaign) -> str:
    brief = (campaign.pending_session_brief or "").strip()
    if not brief:
        return ""
    return f"Tonight's session brief (written by the player before play):\n{brief}"


def build_system_prompt(campaign: Campaign | None) -> str:
    if campaign is None:
        return _BASE_SYSTEM + "\n\nNo campaign context was provided — make reasonable assumptions from the transcript alone."

    parts = [_BASE_SYSTEM, "", "=== CAMPAIGN CONTEXT ===", f"Campaign: {campaign.name} ({campaign.system})"]
    if campaign.setting:
        parts.append(f"Setting: {campaign.setting}")

    parts.append("")
    parts.append("The player you are writing for:")
    parts.append(_character_block(campaign))

    if campaign.perspective_notes:
        parts.append("")
        parts.append(f"Player's personal emphasis: {campaign.perspective_notes}")

    for block in (_party_block(campaign), _npc_roster(campaign),
                  _location_roster(campaign), _plot_threads_block(campaign),
                  _state_block(campaign), _brief_block(campaign)):
        if block:
            parts.append("")
            parts.append(block)

    return "\n".join(parts)


# ── Pass 1 prompts (speaker summaries + utterance classification) ────────────

_PASS1_SYSTEM = """You are analyzing a diarized transcript from a tabletop D&D session. The transcript contains crosstalk, rules lookups, jokes, and off-topic chatter alongside the actual game.

Your job is two things, both returned in one JSON response:

1. **Per-speaker summaries.** For every distinct SPEAKER_XX id that appears, produce:
   - summary: ONE sentence (<=160 chars) describing what that speaker did in this session. Be specific — "narrated a tavern encounter and ran Volo as an NPC" beats "talked a lot." Prefer in-character actions over meta-chatter.
   - role_guess: "DM" | "player" | "unknown". DM heuristics: heavy narration, describing scenes, running multiple NPC voices, calling for rolls. Player heuristics: first-person in-character speech, asking "can I…", declaring actions for one character.
   - roster_guess: if the speaker's utterances strongly match a player character from the campaign party roster (by name, class, or explicit self-identification like "my wizard Thoren"), return that character's exact name from the roster. Otherwise return empty string. Do NOT guess — only fill when confident.
   - sample_quote_indices: pick 2 or 3 transcript-line indices (0-based) that best represent this speaker. Prefer clear, in-character lines that aren't one-word replies.
   - utterance_count and total_seconds will be computed from the transcript — still return a placeholder 0; the backend will overwrite.

2. **Per-utterance classification.** For EVERY transcript line, return one tag:
   - "in_character": on-topic D&D content. In-character dialogue, GM narration, combat resolution, declared actions, descriptions of scenes, plot beats, spells being cast, exploration, roleplay.
   - "other": anything else. Rules lookups ("what's the DC?"), dice math, bathroom breaks, pizza orders, jokes between players, real-world tangents, app/tech troubleshooting.
   When uncertain, prefer "in_character" — we'd rather keep ambiguous content than drop a real plot beat.

Output ONLY JSON matching the supplied Pass1Result schema. Every transcript line index MUST appear exactly once in the tags array."""


def build_pass1_system_prompt(campaign: Campaign | None) -> str:
    """System prompt for the Pass 1 call — includes enough campaign roster for roster_guess matching."""
    parts = [_PASS1_SYSTEM]
    if campaign is None:
        return "\n".join(parts)

    parts.append("\n=== CAMPAIGN CONTEXT (for roster_guess matching) ===")
    parts.append(f"Campaign: {campaign.name} ({campaign.system})")

    # Just the names/classes needed for roster matching — keep this prompt compact
    parts.append("\nThe player (likely one of the speakers):")
    parts.append(f"  - {campaign.player.name}"
                 + (f" — {campaign.player.char_class}" if campaign.player.char_class else ""))

    if campaign.party:
        parts.append("\nOther PCs in the party (possible speaker matches):")
        for c in campaign.party:
            desc = " ".join(filter(None, [c.race, c.char_class, c.subclass])).strip()
            tag = f" — {desc}" if desc else ""
            parts.append(f"  - {c.name}{tag}")

    return "\n".join(parts)


def build_pass1_user_prompt(transcript_lines: list[str]) -> str:
    """
    transcript_lines: pre-rendered strings, one per canonical utterance, e.g.
        "[0] SPEAKER_00: Welcome to the Yawning Portal, travelers."
    The index prefix in square brackets is what the model references for
    sample_quote_indices and tags[].index.
    """
    body = "Here is the diarized transcript. Each line is prefixed with its index:\n\n"
    body += "\n".join(transcript_lines)
    body += (
        "\n\nProduce the Pass1Result JSON. "
        "Remember: EVERY index 0..{last} must appear exactly once in tags[].index."
    ).format(last=len(transcript_lines) - 1 if transcript_lines else 0)
    return body


def build_user_prompt(transcript: str, existing_notes_json: str = "", mode: str = "full") -> str:
    """
    mode: "full" — full session, canonical pass
          "preview" — mid-session partial transcript, notes will be refined later
    """
    header = (
        "Here is the session transcript so far (most recent material at the end)."
        if mode == "preview"
        else "Here is the FULL session transcript."
    )
    body = f"{header}\n\n{transcript}"
    if existing_notes_json:
        body += (
            "\n\nHere are the notes extracted from earlier passes in this same session. "
            "PRESERVE what is still correct; ADD new information from the transcript above. "
            "Do not drop NPCs, locations, or plot points that already appeared:\n\n"
            + existing_notes_json
        )
    body += (
        "\n\nExtract structured notes per the schema. "
        "Remember: these notes are written for the player, in second person, "
        "weighted toward what their character would care about."
    )
    return body
