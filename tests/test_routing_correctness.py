"""
Routing correctness tests for the Interaction Agent.

Hypothesis: as the roster grows, the model picks near-miss / new names instead
of the exact existing agent name, causing duplicate spawns.

What is measured (not asserted, except where noted):
  - duplicate_spawn_rate  (primary): fraction of trials where a new name was emitted
  - exact_reuse_accuracy           : inverse — fraction of verbatim matches
  - wrong_agent_rate               : model chose a *different* existing agent

Variables swept:
  - roster_size N: 5 → 500
  - target position: top / middle / bottom of the list
  - distractor composition: neutral vs. near-miss heavy

Run as pytest to get pass/fail on baseline and per-size data.
Run directly (`python tests/test_routing_correctness.py`) for a full CSV sweep.
"""

from __future__ import annotations

import asyncio
import json
import os
import sys
from dataclasses import dataclass
from html import escape
from pathlib import Path
from typing import Dict, List, Optional, Tuple

import pytest

# ---------------------------------------------------------------------------
# Path setup — lets the tests import from the server package
# ---------------------------------------------------------------------------
ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from server.openrouter_client import request_chat_completion  # noqa: E402

# ---------------------------------------------------------------------------
# System prompt — loaded from the real file so the test context matches prod
# ---------------------------------------------------------------------------
_PROMPT_PATH = ROOT / "server" / "agents" / "interaction_agent" / "system_prompt.md"
SYSTEM_PROMPT = _PROMPT_PATH.read_text(encoding="utf-8").strip()

# ---------------------------------------------------------------------------
# Eval set: (user_request, expected_agent_name)
#
# Each request maps unambiguously to one pre-existing agent.  The expected
# name is what the model must emit verbatim for a "reuse" verdict.
# ---------------------------------------------------------------------------
EVAL_SET: List[Tuple[str, str]] = [
    ("Let Alice know I'll be running late to the meeting", "Email to Alice"),
    ("Send Bob the project report we discussed", "Email to Bob"),
    ("Draft a reply to Carol about the budget proposal", "Reply to Carol"),
    ("Follow up with David about the invoice from last week", "Invoice Follow-up David"),
    ("Tell the team tomorrow's standup is cancelled", "Team Standup Update"),
]

# Near-miss distractors: intentionally similar to eval-set target names
_NEAR_MISS: List[str] = [
    "Email Alice",
    "Message to Alice",
    "Email to Alicia",
    "Send to Bob",
    "Bob Project Report",
    "Carol Budget",
    "Reply Carol",
    "David Invoice",
    "Follow Up with Dave",
    "Standup Message",
    "Team Update",
    "Meeting Update",
    "Alice Email",
    "Email Bob Project",
]

# Neutral distractors: unrelated names that just pad roster size
_NEUTRAL: List[str] = [
    "Hotel Booking Paris",
    "Dentist Appointment",
    "Tax Return 2024",
    "Flight Search NYC",
    "Grocery List",
    "Monthly Report Finance",
    "Presentation Slides Q3",
    "Code Review PR-42",
    "Bug Fix Login Page",
    "Subscription Renewal",
    "Conference Call Notes",
    "Product Launch Plan",
    "Customer Feedback Analysis",
    "Job Application Google",
    "Resume Update",
    "Visa Application",
    "Insurance Claim",
    "Bank Transfer",
    "Rent Payment",
    "Utility Bills",
    "Travel Itinerary",
    "Contract Review",
    "Expense Report",
    "Performance Review",
    "Meeting Notes Q2",
]

# Tool schemas: use the full production set so model context matches reality
_TOOL_SCHEMAS = [
    {
        "type": "function",
        "function": {
            "name": "send_message_to_agent",
            "description": (
                "Deliver instructions to a specific execution agent. "
                "Creates a new agent if the name doesn't exist in the roster, "
                "or reuses an existing one."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "agent_name": {
                        "type": "string",
                        "description": (
                            "Human-readable agent name describing its purpose "
                            "(e.g., 'Vercel Job Offer', 'Email to Sharanjeet'). "
                            "This name will be used to identify and potentially reuse the agent."
                        ),
                    },
                    "instructions": {
                        "type": "string",
                        "description": "Instructions for the agent to execute.",
                    },
                },
                "required": ["agent_name", "instructions"],
                "additionalProperties": False,
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "send_message_to_user",
            "description": (
                "Deliver a natural-language response directly to the user. "
                "Use this for updates, confirmations, or any assistant response "
                "the user should see immediately."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "message": {
                        "type": "string",
                        "description": "Plain-text message shown to the user.",
                    },
                },
                "required": ["message"],
                "additionalProperties": False,
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "send_draft",
            "description": "Record an email draft so the user can review the exact text.",
            "parameters": {
                "type": "object",
                "properties": {
                    "to": {"type": "string"},
                    "subject": {"type": "string"},
                    "body": {"type": "string"},
                },
                "required": ["to", "subject", "body"],
                "additionalProperties": False,
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "wait",
            "description": (
                "Wait silently when a message is already in conversation history "
                "to avoid duplicating responses."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "reason": {"type": "string"},
                },
                "required": ["reason"],
                "additionalProperties": False,
            },
        },
    },
]

# ---------------------------------------------------------------------------
# Roster helpers
# ---------------------------------------------------------------------------

def _make_distractors(n: int, exclude: List[str], near_miss_first: bool = True) -> List[str]:
    """Return n distractor names, optionally near-miss-heavy, excluding `exclude`."""
    near = [d for d in _NEAR_MISS if d not in exclude]
    neutral = [d for d in _NEUTRAL if d not in exclude]
    pool = (near + neutral) if near_miss_first else (neutral + near)
    # Pad with generated names if roster size exceeds the pool
    i = 0
    while len(pool) < n:
        pool.append(f"Misc Task {i:04d}")
        i += 1
    return pool[:n]


def _build_roster(target: str, total_size: int, position: str) -> List[str]:
    """
    Build a roster of `total_size` names.  `target` is placed at:
      "top"    — index 0
      "bottom" — last index
      "middle" — midpoint
    """
    distractors = _make_distractors(total_size - 1, exclude=[target])
    if position == "top":
        return [target] + distractors
    if position == "bottom":
        return distractors + [target]
    mid = len(distractors) // 2
    return distractors[:mid] + [target] + distractors[mid:]


def _render_roster(agents: List[str]) -> str:
    return "\n".join(f'<agent name="{escape(a, quote=True)}" />' for a in agents)


def _build_messages(user_request: str, roster: List[str]) -> List[Dict]:
    """
    Replicate the production prepare_message_with_history() format but with an
    injected roster — no filesystem reads, no singleton state, fully isolated.
    """
    active = _render_roster(roster) if roster else "None"
    content = (
        "<conversation_history>\nNone\n</conversation_history>\n\n"
        f"<active_agents>\n{active}\n</active_agents>\n\n"
        f"<new_user_message>\n{user_request}\n</new_user_message>"
    )
    return [{"role": "user", "content": content}]


# ---------------------------------------------------------------------------
# Response parsing
# ---------------------------------------------------------------------------

def _extract_agent_name(response: Dict) -> Optional[str]:
    """Pull agent_name from the first send_message_to_agent tool call."""
    choice = (response.get("choices") or [{}])[0]
    message = choice.get("message", {})
    for tc in message.get("tool_calls") or []:
        fn = tc.get("function", {})
        if fn.get("name") == "send_message_to_agent":
            raw = fn.get("arguments", "{}")
            try:
                args = json.loads(raw) if isinstance(raw, str) else raw
                return args.get("agent_name")
            except (json.JSONDecodeError, AttributeError):
                return None
    return None


# ---------------------------------------------------------------------------
# Trial execution + classification
# ---------------------------------------------------------------------------

@dataclass
class TrialResult:
    request: str
    expected: str
    emitted: Optional[str]
    roster_size: int
    position: str
    # "reuse" | "spawn" | "wrong_agent" | "no_tool_call"
    outcome: str

    @property
    def is_pass(self) -> bool:
        return self.outcome == "reuse"


def _classify(emitted: Optional[str], expected: str, roster: List[str]) -> str:
    if emitted is None:
        return "no_tool_call"
    if emitted == expected:
        return "reuse"
    if emitted in roster:
        return "wrong_agent"
    return "spawn"  # new name not in roster = duplicate spawn


_MODEL = os.getenv("TEST_MODEL", "google/gemma-4-31b-it:free")
_API_KEY = os.getenv("OPENROUTER_API_KEY")

# Per-day free-tier limit on OpenRouter.  Tests stop early rather than fail
# with a confusing error when the quota is exhausted.
_RATE_LIMIT_SKIP_MSG = (
    "OpenRouter free-tier daily limit reached (50 req/day). "
    "Add credits or wait until UTC midnight to continue."
)


def _is_rate_limit(exc: Exception) -> bool:
    return "429" in str(exc) or "Rate limit" in str(exc)


async def _run_trial(
    request: str,
    expected: str,
    roster_size: int,
    position: str,
    roster: Optional[List[str]] = None,
) -> TrialResult:
    if roster is None:
        roster = _build_roster(expected, roster_size, position)
    messages = _build_messages(request, roster)
    try:
        response = await request_chat_completion(
            model=_MODEL,
            messages=messages,
            system=SYSTEM_PROMPT,
            api_key=_API_KEY,
            tools=_TOOL_SCHEMAS,
        )
    except Exception as exc:
        if _is_rate_limit(exc):
            pytest.skip(_RATE_LIMIT_SKIP_MSG)
        raise
    emitted = _extract_agent_name(response)
    outcome = _classify(emitted, expected, roster)
    return TrialResult(
        request=request,
        expected=expected,
        emitted=emitted,
        roster_size=roster_size,
        position=position,
        outcome=outcome,
    )


def _print_results(results: List[TrialResult]) -> None:
    for r in results:
        tag = "PASS" if r.is_pass else "FAIL"
        print(
            f"  [{tag}] {r.outcome:12s} | "
            f"expected={r.expected!r:35s} | "
            f"emitted={r.emitted!r}"
        )


def _summarise(results: List[TrialResult]) -> Tuple[float, float, float]:
    """Return (accuracy, spawn_rate, wrong_agent_rate)."""
    n = len(results)
    acc = sum(r.is_pass for r in results) / n
    sr = sum(r.outcome == "spawn" for r in results) / n
    wr = sum(r.outcome == "wrong_agent" for r in results) / n
    return acc, sr, wr


# ---------------------------------------------------------------------------
# pytest tests
# ---------------------------------------------------------------------------

_SKIP = pytest.mark.skipif(not _API_KEY, reason="OPENROUTER_API_KEY not set")

# Sizes used in pytest (each size × 5 eval items = 5 calls; keep total ≤ ~20 calls
# so the suite fits within the free-tier daily limit alongside normal app usage).
# The full sweep (5→500 × 3 positions) runs via `python tests/test_routing_correctness.py`.
_ROSTER_SIZES = [5, 25, 100]


@_SKIP
@pytest.mark.asyncio
async def test_baseline_small_roster():
    """
    N=5 baseline.  If accuracy < 80% here, the model is too weak to isolate
    roster-size as the cause of duplicates — report that and skip the sweep.
    """
    results = [
        await _run_trial(req, exp, roster_size=5, position="middle")
        for req, exp in EVAL_SET
    ]
    _print_results(results)
    acc, sr, wr = _summarise(results)
    print(f"\nBaseline N=5: accuracy={acc:.0%}  spawn_rate={sr:.0%}  wrong_agent={wr:.0%}")

    assert acc >= 0.80, (
        f"Baseline accuracy {acc:.0%} < 80% — model weakness, not roster overload. "
        "Consider a stronger model via TEST_MODEL env var before running the sweep."
    )


@_SKIP
@pytest.mark.asyncio
@pytest.mark.parametrize("roster_size", _ROSTER_SIZES)
async def test_reuse_accuracy_by_roster_size(roster_size: int):
    """
    Sweep N from 5 to 500 with target always at the middle.
    Does NOT enforce a threshold — we are measuring, not gating.
    The test fails only if the model produces zero tool calls.
    """
    results = [
        await _run_trial(req, exp, roster_size=roster_size, position="middle")
        for req, exp in EVAL_SET
    ]
    _print_results(results)
    acc, sr, wr = _summarise(results)
    print(
        f"\nN={roster_size}: accuracy={acc:.0%}  "
        f"spawn_rate={sr:.0%}  wrong_agent={wr:.0%}"
    )

    any_tool_call = any(r.emitted is not None for r in results)
    assert any_tool_call, (
        f"Model produced zero send_message_to_agent calls at N={roster_size}. "
        "Check model, API key, or tool schema."
    )


@_SKIP
@pytest.mark.asyncio
@pytest.mark.parametrize("position", ["top", "middle", "bottom"])
async def test_position_sensitivity(position: str):
    """
    N=50, vary where the target sits in the list.
    Detects 'lost in the middle' fragility: if bottom accuracy << top,
    the flat list representation is structurally fragile at scale.
    """
    results = [
        await _run_trial(req, exp, roster_size=50, position=position)
        for req, exp in EVAL_SET
    ]
    _print_results(results)
    acc, sr, wr = _summarise(results)
    print(
        f"\nN=50 position={position}: accuracy={acc:.0%}  "
        f"spawn_rate={sr:.0%}  wrong_agent={wr:.0%}"
    )
    # No threshold — diagnostic only.  Compare top vs bottom in the output.


@_SKIP
@pytest.mark.asyncio
async def test_near_miss_confusion():
    """
    N=15 with only near-miss distractors (no neutral padding).
    Small roster, maximally confusing names — isolates name-similarity as a
    factor independent of roster length.
    """
    results = []
    for req, exp in EVAL_SET[:3]:  # subset to contain API cost
        near_miss_pool = [d for d in _NEAR_MISS if d != exp][:14]
        roster = [exp] + near_miss_pool
        result = await _run_trial(req, exp, roster_size=len(roster), position="top", roster=roster)
        results.append(result)

    _print_results(results)
    acc, sr, wr = _summarise(results)
    print(f"\nNear-miss N=15: accuracy={acc:.0%}  spawn_rate={sr:.0%}  wrong_agent={wr:.0%}")

    # If spawn_rate > 0 here, confusing names alone are enough to break routing —
    # that's a different problem than roster overload.


# ---------------------------------------------------------------------------
# Direct sweep runner — full CSV output
# ---------------------------------------------------------------------------

async def _full_sweep() -> None:
    if not _API_KEY:
        print("OPENROUTER_API_KEY not set — cannot run sweep.")
        return

    print(f"Model: {_MODEL}\n")
    header = f"{'N':>6}  {'pos':8}  {'outcome':12}  {'expected':35}  emitted"
    print(header)
    print("-" * len(header))

    aggregated: Dict[Tuple[int, str], List[TrialResult]] = {}

    for size in _ROSTER_SIZES:
        for pos in ["top", "middle", "bottom"]:
            for req, exp in EVAL_SET:
                r = await _run_trial(req, exp, size, pos)
                aggregated.setdefault((size, pos), []).append(r)
                print(f"{size:>6}  {pos:8}  {r.outcome:12}  {r.expected:35}  {r.emitted!r}")

    print("\n--- Summary ---")
    print(f"{'N':>6}  {'pos':8}  {'accuracy':>8}  {'spawn%':>8}  {'wrong%':>8}")
    for (size, pos), results in sorted(aggregated.items()):
        acc, sr, wr = _summarise(results)
        print(f"{size:>6}  {pos:8}  {acc:>8.0%}  {sr:>8.0%}  {wr:>8.0%}")


if __name__ == "__main__":
    asyncio.run(_full_sweep())
