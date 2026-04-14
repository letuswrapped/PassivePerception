"""LLM system prompt for D&D note extraction."""

SYSTEM_PROMPT = """You are a D&D session scribe. Your job is to read a transcript of a tabletop RPG session and extract structured notes from it.

The transcript may contain speech recognition errors, crosstalk, and out-of-character discussion (rules lookups, jokes, side conversations). Focus on in-character events and story content.

Extract the following from the transcript:

1. **Session Summary** — A 2-4 sentence narrative summary of what happened this session. Write in past tense. Focus on story events, not meta discussion.

2. **NPCs** — Any named non-player characters mentioned or encountered. For each:
   - name: Their name (preserve unusual fantasy spelling if it appears multiple times consistently)
   - description: Brief physical or personality description if given
   - relationship: Their relationship to the party (ally, enemy, neutral, unknown)
   - first_seen: Brief context of when they first appeared (e.g. "tavern scene, session start")
   - last_seen: Brief context of their most recent appearance
   - notes: Any other relevant details (motivations, secrets hinted at, etc.)

3. **Locations** — Any named places mentioned or visited. For each:
   - name: Place name
   - description: What it's like
   - significance: Why it matters to the story

4. **Plot Points** — The 3-7 most significant story beats or revelations. For each:
   - summary: One sentence describing what happened
   - npcs_involved: List of NPC names involved
   - context: Why this matters for the story

5. **Open Questions** — Unresolved mysteries, hooks, or questions raised this session that haven't been answered. Each should be a single sentence starting with a question word or "Who/What/Where/Why/How".

Rules:
- Do NOT invent details not in the transcript. If something is unclear, omit it.
- If the same NPC appears with slight name variations, consolidate them into one entry using the most complete/consistent name.
- Ignore meta-talk: rules questions, dice rolls, bathroom breaks, "wait what page is that on", etc.
- Output ONLY valid JSON matching the schema below. No explanation, no markdown fences.
- EVERY field is REQUIRED. You MUST include all 5 top-level keys, even if some are empty.

JSON schema (follow this EXACTLY):
{
  "summary": "string (REQUIRED - 2-4 sentence narrative summary of the session)",
  "npcs": [{"name": "string", "description": "string", "relationship": "string", "first_seen": "string", "last_seen": "string", "notes": "string"}],
  "locations": [{"name": "string", "description": "string", "significance": "string"}],
  "plot_points": [{"summary": "string", "npcs_involved": ["string"], "context": "string"}],
  "open_questions": ["string"]
}
"""


def build_messages(transcript: str, existing_notes_json: str = "", player_context: dict | None = None) -> list[dict]:
    """Build the message list for the LLM."""
    system = SYSTEM_PROMPT

    # Add player/character context if provided
    if player_context and any(player_context.get(k) for k in ("player_name", "char_name", "char_bio")):
        ctx_parts = ["\n\nPlayer & Character Context (use this to better identify speakers and story relevance):"]
        if player_context.get("player_name"):
            ctx_parts.append(f"- Player name: {player_context['player_name']}")
        if player_context.get("char_name"):
            char_desc = player_context["char_name"]
            details = filter(None, [player_context.get('char_race'), player_context.get('char_class'), player_context.get('char_subclass')])
            detail_str = ' '.join(details)
            if detail_str:
                char_desc += f" ({detail_str})"
            ctx_parts.append(f"- Character: {char_desc}")
        if player_context.get("multiclass") and player_context.get("multi_class"):
            multi_desc = player_context["multi_class"]
            if player_context.get("multi_subclass"):
                multi_desc += f" ({player_context['multi_subclass']})"
            ctx_parts.append(f"- Multiclass: also a {multi_desc}")
        if player_context.get("char_bio"):
            ctx_parts.append(f"- Bio/Backstory: {player_context['char_bio']}")
        system += "\n".join(ctx_parts)

    user_content = f"Here is the session transcript (most recent portion):\n\n{transcript}"
    if existing_notes_json:
        user_content += (
            f"\n\nHere are the notes extracted from earlier in the session. "
            f"PRESERVE all existing information and ADD any new details from "
            f"the transcript above. Do not remove NPCs, locations, or plot points "
            f"that were identified earlier:\n\n{existing_notes_json}"
        )
    user_content += (
        "\n\nExtract structured notes from this transcript. "
        "Start with the summary field first, then npcs, locations, plot_points, and open_questions. "
        "ALL 5 fields are required in your JSON output."
    )
    return [
        {"role": "system", "content": system},
        {"role": "user", "content": user_content},
    ]
