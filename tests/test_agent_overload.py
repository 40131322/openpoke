"""
test_agent_overload.py — Execution-Agent roster overload: measure → mitigate → measure.

Experimental design
-------------------
Hold the MODEL, SYSTEM PROMPT, EVAL SET, and CLASSIFIER fixed. Vary exactly one
thing: the *strategy* that decides which agents from the full roster are shown to
the Interaction Agent. A strategy is:

    retrieve(roster: List[Agent], request: str, k: int) -> List[Agent]

  - "full"          : show everything (baseline / the current behaviour)
  - "semantic_topk" : rank by relevance of (name+description) to request, take k
  - "recency_topk"  : keep the k most-recently-used agents (a "hot cache")
  - "random_topk"   : control — random k (floor; relevance-blind)

Because only the shown subset changes, any movement in spawn-rate, reuse accuracy,
or prompt tokens is attributable to the strategy. This is the before/after rig.

Usage
-----
  Baseline:   python test_agent_overload.py --strategy full
  Mitigated:  python test_agent_overload.py --strategy semantic_topk --topk 15
  A/B (same model, delta table, the headline use case):
              python test_agent_overload.py --compare full semantic_topk

  Offline scaffolding check (no API, fake model):
              python test_agent_overload.py --compare full semantic_topk recency_topk --mock

Plug in YOUR solution: either add a function to RETRIEVERS below, or have your
production retriever produce the subset and pass it through. The test does not
care how retrieval works in prod — it only measures what the model does with the
subset that retrieval would have produced.

Env: OPENROUTER_API_KEY (real runs), TEST_MODEL (default below).
"""

from __future__ import annotations

import argparse
import asyncio
import csv
import datetime
import json
import math
import os
import random
import re
import subprocess
import sys
from collections import Counter
from dataclasses import asdict, dataclass, field
from html import escape
from pathlib import Path
from typing import Callable, Dict, List, Optional, Tuple

class RateLimitError(Exception):
    """Raised when OpenRouter returns 429. Carries the UTC reset time."""

    def __init__(self, message: str, reset_ts_ms: Optional[int] = None) -> None:
        super().__init__(message)
        self.reset_ts_ms = reset_ts_ms

    @property
    def reset_utc(self) -> str:
        if self.reset_ts_ms:
            dt = datetime.datetime.fromtimestamp(self.reset_ts_ms / 1000, tz=datetime.timezone.utc)
            return dt.strftime("%Y-%m-%d %H:%M UTC")
        return "unknown"


# ---------------------------------------------------------------------------
# Optional integration with the real codebase. Falls back to a self-contained
# OpenRouter client so this file runs standalone, anywhere.
# ---------------------------------------------------------------------------
ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

try:
    from server.openrouter_client import request_chat_completion as _real_completion  # type: ignore
    _HAVE_REAL_CLIENT = True
except Exception:
    _real_completion = None
    _HAVE_REAL_CLIENT = False

# Load model + API key from the project's real config (which also reads .env).
# TEST_MODEL env var overrides everything so a single run can target a different
# model without touching config.py or .env.
try:
    from server.config import get_settings as _get_settings  # type: ignore
    _settings = _get_settings()
    _MODEL = os.getenv("TEST_MODEL") or _settings.interaction_agent_model
    _API_KEY = _settings.openrouter_api_key or os.getenv("OPENROUTER_API_KEY")
except Exception:
    _MODEL = os.getenv("TEST_MODEL", "google/gemma-4-31b-it:free")
    _API_KEY = os.getenv("OPENROUTER_API_KEY")

# System prompt: load the real one if present, else a faithful stand-in so the
# standalone run still exercises the same routing instruction.
_PROMPT_PATH = Path(__file__).resolve().parent.parent / "server" / "agents" / "interaction_agent" / "system_prompt.md"
if _PROMPT_PATH.exists():
    SYSTEM_PROMPT = _PROMPT_PATH.read_text(encoding="utf-8").strip()
else:
    SYSTEM_PROMPT = (
        "You are the Interaction Agent. You route user requests to execution agents. "
        "If an existing agent in <active_agents> already covers the request, reuse it by "
        "calling send_message_to_agent with its EXACT name. Only invent a new agent name "
        "when no existing agent fits."
    )


# ---------------------------------------------------------------------------
# Roster model: agents now carry a description and a recency signal, so semantic
# and hot-cache strategies have something real to work with.
# ---------------------------------------------------------------------------
@dataclass
class Agent:
    name: str
    description: str
    last_used_days_ago: int = 0  # 0 = used today; larger = more dormant


@dataclass
class EvalItem:
    request: str
    expected: str  # the agent name the model must emit verbatim to count as reuse


EVAL_SET: List[EvalItem] = [
    EvalItem("Let Alice know I'll be running late to the meeting", "Email to Alice"),
    EvalItem("Send Bob the project report we discussed", "Email to Bob"),
    EvalItem("Draft a reply to Carol about the budget proposal", "Reply to Carol"),
    EvalItem("Follow up with David about the invoice from last week", "Invoice Follow-up David"),
    EvalItem("Tell the team tomorrow's standup is cancelled", "Team Standup Update"),
]

# Descriptions for the five target agents (what retrieval must surface).
_TARGET_DESC: Dict[str, str] = {
    "Email to Alice": "Compose and send emails to Alice about meetings, scheduling, and timing.",
    "Email to Bob": "Send project reports, documents, and deliverables to Bob.",
    "Reply to Carol": "Draft replies to Carol regarding budgets, proposals, and approvals.",
    "Invoice Follow-up David": "Follow up with David on outstanding invoices and payments.",
    "Team Standup Update": "Post standup notes and schedule changes to the team channel.",
}

# Near-miss distractors: deliberately semantically adjacent to the targets, so a
# relevance ranker can be fooled into ranking them above the true target. This is
# where a semantic strategy shows its *limit*.
_NEAR_MISS: List[Tuple[str, str]] = [
    ("Email Alice", "Quick one-line emails to Alice."),
    ("Message to Alice", "Send chat messages to Alice."),
    ("Email to Alicia", "Send emails to Alicia in accounting."),
    ("Send to Bob", "Hand off files to Bob."),
    ("Bob Project Report", "Assemble Bob's weekly project report."),
    ("Carol Budget", "Track Carol's budget spreadsheet."),
    ("Reply Carol", "Acknowledge Carol's messages."),
    ("David Invoice", "Generate invoices for David."),
    ("Follow Up with Dave", "Check in with Dave about the contract."),
    ("Standup Message", "Write the daily standup message."),
    ("Team Update", "Broadcast general updates to the team."),
    ("Meeting Update", "Notify attendees of meeting changes."),
    ("Alice Email", "Archive of emails from Alice."),
    ("Email Bob Project", "Email Bob about project status."),
]

# Neutral distractors: unrelated padding to grow the roster.
_NEUTRAL: List[Tuple[str, str]] = [
    ("Hotel Booking Paris", "Reserve hotels for the Paris trip."),
    ("Dentist Appointment", "Schedule and reschedule dentist visits."),
    ("Tax Return 2024", "Prepare the 2024 tax return."),
    ("Flight Search NYC", "Find flights to New York."),
    ("Grocery List", "Maintain the weekly grocery list."),
    ("Monthly Report Finance", "Compile the monthly finance report."),
    ("Presentation Slides Q3", "Build the Q3 presentation deck."),
    ("Code Review PR-42", "Review pull request 42."),
    ("Bug Fix Login Page", "Fix the login page bug."),
    ("Subscription Renewal", "Renew software subscriptions."),
    ("Conference Call Notes", "Take notes during conference calls."),
    ("Product Launch Plan", "Plan the product launch."),
    ("Customer Feedback Analysis", "Analyze customer feedback surveys."),
    ("Job Application Google", "Track the Google job application."),
    ("Resume Update", "Update the resume."),
    ("Visa Application", "Handle the visa application."),
    ("Insurance Claim", "File the insurance claim."),
    ("Bank Transfer", "Set up bank transfers."),
    ("Rent Payment", "Pay monthly rent."),
    ("Utility Bills", "Pay utility bills."),
    ("Travel Itinerary", "Plan the travel itinerary."),
    ("Contract Review", "Review legal contracts."),
    ("Expense Report", "Submit expense reports."),
    ("Performance Review", "Prepare the performance review."),
    ("Meeting Notes Q2", "Q2 meeting notes."),
]


# ---------------------------------------------------------------------------
# Roster construction
# ---------------------------------------------------------------------------
def build_roster(
    target: str,
    total_size: int,
    *,
    target_recency_days: int,
    rng: random.Random,
) -> List[Agent]:
    """
    Build a roster of `total_size` agents containing `target`. Distractors get
    recency values spread across [0, 60] days so a hot-cache strategy has a real
    ordering to work with. The target's recency is set explicitly via
    `target_recency_days` — set it high to test the hot-cache *limit* (a dormant
    but still-needed agent gets evicted from the cache).
    """
    target_agent = Agent(target, _TARGET_DESC[target], last_used_days_ago=target_recency_days)

    pool = [Agent(n, d) for n, d in (_NEAR_MISS + _NEUTRAL) if n != target]
    i = 0
    while len(pool) < total_size - 1:
        pool.append(Agent(f"Misc Task {i:04d}", f"Miscellaneous task number {i}."))
        i += 1
    distractors = pool[: total_size - 1]
    for a in distractors:
        a.last_used_days_ago = rng.randint(0, 60)

    roster = distractors + [target_agent]
    rng.shuffle(roster)
    return roster


# ---------------------------------------------------------------------------
# Strategies (the swap point). Each returns the SUBSET shown to the model.
# Add your production solution here, or feed its output in directly.
# ---------------------------------------------------------------------------
_WORD = re.compile(r"[a-z0-9]+")


def _tokens(text: str) -> List[str]:
    return _WORD.findall(text.lower())


def _lexical_score(query_tokens: Counter, doc: str) -> float:
    """Cosine over raw term counts. Stand-in for real embeddings — replace the
    body of semantic_topk with your embedding similarity for a faithful test."""
    d = Counter(_tokens(doc))
    if not d or not query_tokens:
        return 0.0
    dot = sum(query_tokens[t] * d[t] for t in query_tokens)
    nq = math.sqrt(sum(v * v for v in query_tokens.values()))
    nd = math.sqrt(sum(v * v for v in d.values()))
    return dot / (nq * nd) if nq and nd else 0.0


def retrieve_full(roster: List[Agent], request: str, k: int) -> List[Agent]:
    return list(roster)


def retrieve_semantic_topk(roster: List[Agent], request: str, k: int) -> List[Agent]:
    q = Counter(_tokens(request))
    scored = sorted(
        roster,
        key=lambda a: _lexical_score(q, f"{a.name} {a.description}"),
        reverse=True,
    )
    return scored[:k]


def retrieve_recency_topk(roster: List[Agent], request: str, k: int) -> List[Agent]:
    return sorted(roster, key=lambda a: a.last_used_days_ago)[:k]


def retrieve_random_topk(roster: List[Agent], request: str, k: int) -> List[Agent]:
    pool = list(roster)
    random.shuffle(pool)
    return pool[:k]


RETRIEVERS: Dict[str, Callable[[List[Agent], str, int], List[Agent]]] = {
    "full": retrieve_full,
    "semantic_topk": retrieve_semantic_topk,
    "recency_topk": retrieve_recency_topk,
    "random_topk": retrieve_random_topk,
}


# ---------------------------------------------------------------------------
# Prompt assembly (held constant across strategies)
# ---------------------------------------------------------------------------
def render_roster(agents: List[Agent]) -> str:
    return "\n".join(
        f'<agent name="{escape(a.name, quote=True)}" '
        f'description="{escape(a.description, quote=True)}" />'
        for a in agents
    )


def build_messages(request: str, shown: List[Agent]) -> List[Dict]:
    active = render_roster(shown) if shown else "None"
    content = (
        "<conversation_history>\nNone\n</conversation_history>\n\n"
        f"<active_agents>\n{active}\n</active_agents>\n\n"
        f"<new_user_message>\n{request}\n</new_user_message>"
    )
    return [{"role": "user", "content": content}]


_TOOL_SCHEMAS = [
    {
        "type": "function",
        "function": {
            "name": "send_message_to_agent",
            "description": (
                "Deliver instructions to a specific execution agent. Creates a new "
                "agent if the name doesn't exist, or reuses an existing one."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "agent_name": {"type": "string"},
                    "instructions": {"type": "string"},
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
            "description": "Deliver a natural-language response directly to the user.",
            "parameters": {
                "type": "object",
                "properties": {"message": {"type": "string"}},
                "required": ["message"],
                "additionalProperties": False,
            },
        },
    },
]


# ---------------------------------------------------------------------------
# Token accounting (the cost side of overload)
# ---------------------------------------------------------------------------
try:
    import tiktoken  # type: ignore

    _ENC = tiktoken.get_encoding("cl100k_base")

    def count_tokens(text: str) -> int:
        return len(_ENC.encode(text))
except Exception:
    def count_tokens(text: str) -> int:
        return max(1, len(text) // 4)  # rough fallback


# ---------------------------------------------------------------------------
# Model client
# ---------------------------------------------------------------------------
async def _openrouter_completion(*, model, messages, system, api_key, tools) -> Dict:
    """Self-contained OpenRouter call (OpenAI-compatible shape), no extra deps."""
    import urllib.request

    payload = {
        "model": model,
        "messages": ([{"role": "system", "content": system}] if system else []) + messages,
        "tools": tools,
        "tool_choice": "auto",
    }
    body = json.dumps(payload).encode("utf-8")

    def _do() -> Dict:
        req = urllib.request.Request(
            "https://openrouter.ai/api/v1/chat/completions",
            data=body,
            headers={
                "Authorization": f"Bearer {api_key}",
                "Content-Type": "application/json",
            },
            method="POST",
        )
        with urllib.request.urlopen(req, timeout=120) as resp:
            return json.loads(resp.read().decode("utf-8"))

    return await asyncio.to_thread(_do)


def _mock_completion_factory():
    """A fake model whose accuracy degrades with the number of near-miss agents
    actually shown — purely so the harness can be verified offline. NOT a model."""
    near_miss_names = {n for n, _ in _NEAR_MISS}
    rng = random.Random(0)

    async def _mock(*, model, messages, system, api_key, tools) -> Dict:
        content = messages[-1]["content"]
        shown = re.findall(r'<agent name="([^"]+)"', content)
        request = re.search(r"<new_user_message>\n(.*?)\n</new_user_message>", content, re.S)
        request = request.group(1) if request else ""
        # Which target is expected for this request?
        expected = next((e.expected for e in EVAL_SET if e.request == request), None)
        present = expected in shown
        if not present:
            # Target not in context → model must invent a name. Forced spawn.
            emit = f"{request[:18].strip()} Agent"
        else:
            n_near = sum(1 for s in shown if s in near_miss_names)
            p_correct = max(0.15, 1.0 - 0.05 * n_near)  # more near-misses → worse
            if rng.random() < p_correct:
                emit = expected
            elif n_near and rng.random() < 0.6:
                emit = next(s for s in shown if s in near_miss_names)  # wrong existing
            else:
                emit = f"New {request[:14].strip()}"  # duplicate spawn
        return {
            "choices": [
                {"message": {"tool_calls": [
                    {"function": {"name": "send_message_to_agent",
                                  "arguments": json.dumps({"agent_name": emit, "instructions": "..."})}}
                ]}}
            ]
        }

    return _mock


def _select_client(mock: bool):
    if mock:
        return _mock_completion_factory()
    if _HAVE_REAL_CLIENT:
        return _real_completion
    return _openrouter_completion


# ---------------------------------------------------------------------------
# Trial execution + classification
# ---------------------------------------------------------------------------
@dataclass
class TrialResult:
    expected: str
    emitted: Optional[str]
    roster_size: int
    shown_size: int
    target_shown: bool
    prompt_tokens: int
    outcome: str  # reuse | spawn | wrong_agent | no_tool_call


def _extract_agent_name(response: Dict) -> Optional[str]:
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


def _classify(emitted: Optional[str], expected: str, shown_names: List[str]) -> str:
    if emitted is None:
        return "no_tool_call"
    if emitted == expected:
        return "reuse"
    if emitted in shown_names:
        return "wrong_agent"
    return "spawn"


async def run_trial(client, item: EvalItem, roster: List[Agent], strategy: str, k: int) -> TrialResult:
    shown = RETRIEVERS[strategy](roster, item.request, k)
    shown_names = [a.name for a in shown]
    messages = build_messages(item.request, shown)
    prompt_tokens = count_tokens(SYSTEM_PROMPT) + count_tokens(messages[-1]["content"])
    try:
        response = await client(model=_MODEL, messages=messages, system=SYSTEM_PROMPT, api_key=_API_KEY, tools=_TOOL_SCHEMAS)
    except Exception as exc:
        msg = str(exc)
        if "429" in msg or "Rate limit" in msg:
            m = re.search(r"X-RateLimit-Reset['\"]?\s*:\s*['\"]?(\d+)", msg)
            reset_ms = int(m.group(1)) if m else None
            raise RateLimitError(msg, reset_ts_ms=reset_ms) from exc
        raise
    emitted = _extract_agent_name(response)
    return TrialResult(
        expected=item.expected,
        emitted=emitted,
        roster_size=len(roster),
        shown_size=len(shown),
        target_shown=item.expected in shown_names,
        prompt_tokens=prompt_tokens,
        outcome=_classify(emitted, item.expected, shown_names),
    )


# ---------------------------------------------------------------------------
# Metrics
# ---------------------------------------------------------------------------
@dataclass
class Metrics:
    n: int
    accuracy: float          # reuse rate (the thing you want high)
    spawn_rate: float        # duplicate spawns (the thing you want low)
    wrong_agent_rate: float
    target_recall: float     # did the strategy keep the right agent in context
    avg_prompt_tokens: float


def summarise(results: List[TrialResult]) -> Metrics:
    n = len(results)
    return Metrics(
        n=n,
        accuracy=sum(r.outcome == "reuse" for r in results) / n,
        spawn_rate=sum(r.outcome == "spawn" for r in results) / n,
        wrong_agent_rate=sum(r.outcome == "wrong_agent" for r in results) / n,
        target_recall=sum(r.target_shown for r in results) / n,
        avg_prompt_tokens=sum(r.prompt_tokens for r in results) / n,
    )


# ---------------------------------------------------------------------------
# Sweep + comparison
# ---------------------------------------------------------------------------
async def run_strategy(
    client, strategy: str, sizes: List[int], k: int, trials: int,
    target_recency: int, seed: int,
) -> tuple[Dict[int, Metrics], Optional[RateLimitError]]:
    """Run all sizes for a strategy. Returns (completed_sizes, rate_limit_error_or_None)."""
    out: Dict[int, Metrics] = {}
    for size in sizes:
        results: List[TrialResult] = []
        try:
            for t in range(trials):
                rng = random.Random(seed + t)
                for item in EVAL_SET:
                    roster = build_roster(item.expected, size, target_recency_days=target_recency, rng=rng)
                    results.append(await run_trial(client, item, roster, strategy, k))
        except RateLimitError as exc:
            # Save whatever completed sizes we have; drop the partial size
            return out, exc
        out[size] = summarise(results)
    return out, None


def _fmt_table(strategy: str, by_size: Dict[int, Metrics]) -> str:
    lines = [f"\n=== strategy: {strategy} (model: {_MODEL}) ==="]
    lines.append(f"{'N':>6}  {'accuracy':>9}  {'spawn':>7}  {'wrong':>7}  {'recall':>7}  {'tokens':>8}")
    for size, m in sorted(by_size.items()):
        lines.append(
            f"{size:>6}  {m.accuracy:>8.0%}  {m.spawn_rate:>6.0%}  "
            f"{m.wrong_agent_rate:>6.0%}  {m.target_recall:>6.0%}  {m.avg_prompt_tokens:>8.0f}"
        )
    return "\n".join(lines)


def _fmt_delta(base: str, mit: str, b: Dict[int, Metrics], m: Dict[int, Metrics]) -> str:
    lines = [f"\n=== delta: {mit} vs {base} (positive accuracy / negative spawn = win) ==="]
    lines.append(f"{'N':>6}  {'d_accuracy':>10}  {'d_spawn':>8}  {'d_tokens':>9}")
    for size in sorted(b):
        if size not in m:
            continue
        da = m[size].accuracy - b[size].accuracy
        ds = m[size].spawn_rate - b[size].spawn_rate
        dt = m[size].avg_prompt_tokens - b[size].avg_prompt_tokens
        lines.append(f"{size:>6}  {da:>+9.0%}  {ds:>+7.0%}  {dt:>+9.0f}")
    return "\n".join(lines)


# ---------------------------------------------------------------------------
# Run-artifact persistence + cross-branch diff (what the git workflow needs)
# ---------------------------------------------------------------------------
def _git_info() -> Dict[str, Optional[str]]:
    def _run(cmd):
        try:
            return subprocess.check_output(
                cmd, cwd=Path(__file__).resolve().parent, stderr=subprocess.DEVNULL
            ).decode().strip()
        except Exception:
            return None
    return {
        "sha": _run(["git", "rev-parse", "--short", "HEAD"]),
        "branch": _run(["git", "rev-parse", "--abbrev-ref", "HEAD"]),
        "dirty": bool(_run(["git", "status", "--porcelain"])),
    }


def _write_artifact(path: str, args, all_results: Dict[str, Dict[int, Metrics]]) -> None:
    payload = {
        "meta": {
            **_git_info(),
            "model": _MODEL,
            "seed": args.seed,
            "topk": args.topk,
            "trials": args.trials,
            "sizes": args.sizes,
            "target_recency": args.target_recency,
        },
        "results": {
            strat: {str(size): asdict(m) for size, m in by_size.items()}
            for strat, by_size in all_results.items()
        },
    }
    Path(path).write_text(json.dumps(payload, indent=2))
    print(f"\nWrote {path}  (branch={payload['meta']['branch']} sha={payload['meta']['sha']}"
          f"{' DIRTY' if payload['meta']['dirty'] else ''})")


def _write_csv(path: str, args, all_results: Dict[str, Dict[int, Metrics]]) -> None:
    git = _git_info()
    rows = []
    for strat, by_size in all_results.items():
        for size, m in sorted(by_size.items()):
            rows.append({
                "branch": git["branch"],
                "sha": git["sha"],
                "dirty": git["dirty"],
                "model": _MODEL,
                "strategy": strat,
                "roster_size": size,
                "topk": args.topk,
                "trials": args.trials,
                "target_recency_days": args.target_recency,
                "seed": args.seed,
                "n_trials": m.n,
                "accuracy": round(m.accuracy, 4),
                "spawn_rate": round(m.spawn_rate, 4),
                "wrong_agent_rate": round(m.wrong_agent_rate, 4),
                "target_recall": round(m.target_recall, 4),
                "avg_prompt_tokens": round(m.avg_prompt_tokens, 1),
            })
    fieldnames = list(rows[0].keys()) if rows else []
    with open(path, "w", newline="", encoding="utf-8") as f:
        writer = csv.DictWriter(f, fieldnames=fieldnames)
        writer.writeheader()
        writer.writerows(rows)
    print(f"Wrote {path}  ({len(rows)} rows)")


def cmd_diff(base_path: str, branch_path: str) -> None:
    """Diff two run artifacts (branch − base). Designed for: run on base branch
    with --out base.json, run on solution branch with --out branch.json, then
    `--diff base.json branch.json`."""
    A = json.loads(Path(base_path).read_text())
    B = json.loads(Path(branch_path).read_text())

    def _tag(m):
        return f"branch={m.get('branch')} sha={m.get('sha')}{' DIRTY' if m.get('dirty') else ''} model={m.get('model')}"

    print(f"BASE   ({base_path}): {_tag(A['meta'])}")
    print(f"BRANCH ({branch_path}): {_tag(B['meta'])}")
    if A["meta"].get("model") != B["meta"].get("model"):
        print("!! WARNING: models differ across runs — accuracy deltas are not attributable to the solution.")
    if A["meta"].get("seed") != B["meta"].get("seed"):
        print("!! WARNING: seeds differ — the two runs did not see the same rosters.")

    # If each artifact has exactly one strategy, compare those two regardless of name
    # (the typical base=full vs branch=<solution> case). Otherwise match by name.
    a_strats, b_strats = list(A["results"]), list(B["results"])
    if len(a_strats) == 1 and len(b_strats) == 1:
        pairs = [(a_strats[0], b_strats[0])]
    else:
        common = [s for s in a_strats if s in b_strats]
        pairs = [(s, s) for s in common]

    for sa, sb in pairs:
        print(f"\n=== delta: BRANCH[{sb}] − BASE[{sa}] "
              f"(positive accuracy / negative spawn = win) ===")
        print(f"{'N':>6}  {'d_accuracy':>10}  {'d_spawn':>8}  {'d_recall':>8}  {'d_tokens':>9}")
        sizes = sorted(set(int(k) for k in A['results'][sa]) & set(int(k) for k in B['results'][sb]))
        for size in sizes:
            a = A["results"][sa][str(size)]
            b = B["results"][sb][str(size)]
            print(f"{size:>6}  {b['accuracy']-a['accuracy']:>+9.0%}  "
                  f"{b['spawn_rate']-a['spawn_rate']:>+7.0%}  "
                  f"{b['target_recall']-a['target_recall']:>+7.0%}  "
                  f"{b['avg_prompt_tokens']-a['avg_prompt_tokens']:>+9.0f}")


async def main_async(args) -> None:
    if not args.mock and not _API_KEY:
        print("OPENROUTER_API_KEY not set. Use --mock for an offline scaffolding check.")
        return
    client = _select_client(args.mock)
    sizes = args.sizes

    strategies = args.compare if args.compare else [args.strategy]
    all_results: Dict[str, Dict[int, Metrics]] = {}
    rate_limit: Optional[RateLimitError] = None

    for strat in strategies:
        if strat not in RETRIEVERS:
            print(f"Unknown strategy '{strat}'. Known: {', '.join(RETRIEVERS)}")
            return
        by_size, exc = await run_strategy(
            client, strat, sizes, args.topk, args.trials, args.target_recency, args.seed
        )
        if by_size:
            all_results[strat] = by_size
            print(_fmt_table(strat, by_size))
        if exc:
            rate_limit = exc
            print(f"\n!! Rate limit hit after {len(by_size)} completed roster size(s) for '{strat}'.")
            break  # stop running further strategies

    if rate_limit:
        print(f"   Resets at: {rate_limit.reset_utc}")
        print("   Re-run after that time, or use --mock for an offline check.")

    # If exactly a baseline + mitigations comparison, print deltas vs the first.
    if args.compare and len(args.compare) >= 2 and not rate_limit:
        base = args.compare[0]
        for mit in args.compare[1:]:
            if mit in all_results:
                print(_fmt_delta(base, mit, all_results[base], all_results[mit]))

    if all_results:
        if args.out:
            _write_artifact(args.out, args, all_results)
        if args.csv:
            _write_csv(args.csv, args, all_results)
    elif not rate_limit:
        print("No results to save.")


def parse_args(argv=None):
    p = argparse.ArgumentParser(description="Execution-Agent overload A/B harness.")
    p.add_argument("--strategy", default="full", help="single strategy to run")
    p.add_argument("--compare", nargs="+", help="run multiple strategies; first is baseline for deltas")
    p.add_argument("--sizes", type=int, nargs="+", default=[5, 25, 100, 250],
                   help="roster sizes to sweep")
    p.add_argument("--topk", type=int, default=15, help="k for top-k strategies")
    p.add_argument("--trials", type=int, default=1, help="repeats per (size, eval item)")
    p.add_argument("--target-recency", type=int, default=2,
                   help="days-ago for the target agent; raise it to probe hot-cache limits")
    p.add_argument("--seed", type=int, default=1234)
    p.add_argument("--out", help="write this run's metrics to a JSON artifact (tagged with git sha/branch)")
    p.add_argument("--csv", help="write this run's metrics to a CSV file")
    p.add_argument("--diff", nargs=2, metavar=("BASE_JSON", "BRANCH_JSON"),
                   help="diff two run artifacts (branch − base) and exit; no API calls")
    p.add_argument("--mock", action="store_true", help="offline fake model (scaffolding check only)")
    return p.parse_args(argv)


if __name__ == "__main__":
    _args = parse_args()
    if _args.diff:
        cmd_diff(_args.diff[0], _args.diff[1])
    else:
        asyncio.run(main_async(_args))
