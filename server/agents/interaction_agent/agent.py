"""Interaction agent helpers for prompt construction."""

import math
import re
from collections import Counter
from html import escape
from pathlib import Path
from typing import Dict, List, Tuple

from ...services.execution import get_agent_roster

_prompt_path = Path(__file__).parent / "system_prompt.md"
SYSTEM_PROMPT = _prompt_path.read_text(encoding="utf-8").strip()

# Roster-rendering policy. K matches the value validated in
# tests/test_agent_overload.py (topk=15, recall held at 100%).
ROSTER_SHOW_ALL_THRESHOLD = 30   # below this, show everything (cheap and maximally safe)
ROSTER_RELEVANCE_K        = 15   # top-k by relevance to the current request
ROSTER_PINNED_RECENT      = 5    # always pin the most-recently-used agents

_WORD = re.compile(r"[a-z0-9]+")


def _tokens(text: str) -> Counter:
    return Counter(_WORD.findall(text.lower()))


def _cosine(q: Counter, doc: str) -> float:
    d = _tokens(doc)
    if not q or not d:
        return 0.0
    dot = sum(q[t] * d[t] for t in q)
    nq = math.sqrt(sum(v * v for v in q.values()))
    nd = math.sqrt(sum(v * v for v in d.values()))
    return dot / (nq * nd) if nq and nd else 0.0


def _select_roster(records: List[Dict], query: str) -> Tuple[List[Dict], int]:
    """Relevance top-k UNION pinned-recent. Returns (shown, n_hidden)."""
    total = len(records)
    if total <= ROSTER_SHOW_ALL_THRESHOLD or not query.strip():
        return records, 0

    q = _tokens(query)
    by_relevance = sorted(
        records,
        key=lambda r: _cosine(q, f"{r.get('name', '')} {r.get('purpose', '')}"),
        reverse=True,
    )[:ROSTER_RELEVANCE_K]
    by_recency = sorted(
        records, key=lambda r: r.get("last_used") or "", reverse=True
    )[:ROSTER_PINNED_RECENT]

    shown, seen = [], set()
    for r in (*by_relevance, *by_recency):
        if r["id"] not in seen:
            seen.add(r["id"])
            shown.append(r)
    return shown, total - len(shown)


# Load and return the pre-defined system prompt from markdown file
def build_system_prompt() -> str:
    """Return the static system prompt for the interaction agent."""
    return SYSTEM_PROMPT


# Build structured message with conversation history, active agents, and current turn
def prepare_message_with_history(
    latest_text: str,
    transcript: str,
    message_type: str = "user",
) -> List[Dict[str, str]]:
    """Compose a message that bundles history, roster, and the latest turn."""
    sections: List[str] = []

    sections.append(_render_conversation_history(transcript))
    sections.append(f"<active_agents>\n{_render_active_agents(latest_text)}\n</active_agents>")
    sections.append(_render_current_turn(latest_text, message_type))

    content = "\n\n".join(sections)
    return [{"role": "user", "content": content}]


# Format conversation transcript into XML tags for LLM context
def _render_conversation_history(transcript: str) -> str:
    history = transcript.strip()
    if not history:
        history = "None"
    return f"<conversation_history>\n{history}\n</conversation_history>"


# Filter roster by relevance + recency and format as XML for LLM context
def _render_active_agents(query: str = "") -> str:
    roster = get_agent_roster()
    roster.load()
    records = roster.get_records()

    if not records:
        return "None"

    shown, hidden = _select_roster(records, query)
    lines = [
        f'<agent id="{escape(r.get("id") or "", quote=True)}" '
        f'name="{escape(r.get("name") or "agent", quote=True)}">'
        f'{escape(r.get("purpose") or "")}</agent>'
        for r in shown
    ]
    body = "\n".join(lines)
    if hidden:
        body += (
            f"\n<!-- {hidden} more agent(s) exist but are not shown here. "
            f"Call find_agent to search the full roster by description. -->"
        )
    return body


# Wrap the current message in appropriate XML tags based on sender type
def _render_current_turn(latest_text: str, message_type: str) -> str:
    tag = "new_agent_message" if message_type == "agent" else "new_user_message"
    body = latest_text.strip()
    return f"<{tag}>\n{body}\n</{tag}>"
