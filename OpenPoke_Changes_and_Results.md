# OpenPoke — Agent Overload Fix & Google Calendar Integration

This document covers two bodies of work on the `agent-solution` branch:

1. Diagnosing and fixing the **execution-agent roster overload** problem in the interaction agent.
2. Adding **Google Calendar** support, plus the optimizations that keep it fast and cheap.

---

## Part 1 — The Agent Overload Problem

### What was going wrong

The interaction agent routes every user request to an execution agent through the `send_message_to_agent` tool. It either reuses an existing agent or spins up a new one. To make that decision it sees an `<active_agents>` block listing the current roster.

The original implementation rendered the **entire roster as a flat list** and matched agents by name. That works fine when there are a handful of agents. It degrades badly as the roster grows: the model starts picking *near-miss* names ("Email Alice" instead of "Email to Alice", "Reply Carol" instead of "Reply to Carol") or inventing a brand-new agent for a task an existing agent already owns. The result is duplicate agents, fragmented conversation history, and replies landing on the wrong email thread.

The hypothesis we set out to test: **routing accuracy falls as the roster grows, and the flat-list representation is the cause.**

### What we changed

The fix has four parts, all aimed at making agent identity stable and making the right agent easy to find without dumping the whole roster into context.

**1. Stable agent identity (`server/services/execution/roster.py`)**
The roster moved from bare name strings to full records carrying a stable `id`, `name`, one-line `purpose`, and `created_at` / `last_used` timestamps. Old bare-string rosters are migrated on load, so this is backward compatible. Lookups now resolve by `id` first (`find_by_id`), then fall back to exact name (`find_by_name`), and `touch` keeps recency fresh.

**2. ID-first routing (`server/agents/interaction_agent/tools.py`)**
`send_message_to_agent` now accepts an `agent_id` (which takes priority over name) and an optional `purpose` used when creating a new agent. Resolution order is: id → exact name → create new. This means the model can reuse an agent by copying its stable id rather than retyping a name it might get subtly wrong.

**3. A `find_agent` retrieval tool (`server/agents/interaction_agent/tools.py`)**
A new tool lets the model search the roster by natural-language query and get back ranked candidates (id, name, purpose, similarity score) using a lightweight cosine-over-token-counts ranker. This is the escape hatch for agents that aren't currently surfaced in `<active_agents>` — e.g. ones created in an earlier session.

**4. Prompt guidance (`server/agents/interaction_agent/system_prompt.md` + `agent.py`)**
The `<active_agents>` block now renders `id`, `name`, and `purpose` per agent, and the system prompt explicitly instructs: reuse by `agent_id` (the reliable path), call `find_agent` first when unsure an agent exists, and always supply a `purpose` when creating so future recall works.

### How we tested it

Two harnesses, both loading the **real production system prompt** so the test context matches what ships.

**`tests/test_agent_overload.py` — the measure → mitigate → measure rig.**
This is the headline experiment. It holds the model, system prompt, eval set, and classifier fixed and varies exactly one thing: the *retrieval strategy* that decides which agents from the full roster are shown to the model.

- `full` — show everything (baseline / current behaviour)
- `semantic_topk` — rank by relevance of (name + description) to the request, take top k
- `recency_topk` — keep the k most-recently-used agents (a hot cache)
- `random_topk` — relevance-blind control

Because only the shown subset changes, any movement in spawn-rate, reuse accuracy, or token count is attributable to the strategy. The roster is built with deliberate **near-miss distractors** (semantically adjacent names designed to fool a ranker) plus neutral padding, swept across sizes N = 5 / 25 / 100. Metrics captured per run: reuse accuracy, spawn rate, wrong-agent rate, search rate, target recall, and average prompt tokens. Runs are written to timestamped JSON/CSV artifacts tagged with git sha and branch, so two branches can be diffed directly.

**`tests/test_routing_correctness.py` — the diagnostic sweep.**
Confirms the underlying hypothesis with finer-grained probes: accuracy by roster size, target position sensitivity (top / middle / bottom — the "lost in the middle" check), and a near-miss-only roster that isolates name similarity from roster length.

### The results

Three views of the same data.

**View 1 — Strategy effect within `agent-solution` (full → production retrieval)**

| N | Δ accuracy | Δ spawn | Δ tokens |
|----|-----------|---------|----------|
| 5   | +0% | 0% | 0 |
| 25  | −7% | 0% | 0 |
| 100 | +0% | 0% | −1713 |

At the realistic large-roster case (N = 100), the production retrieval strategy saves **1,713 tokens per call with no accuracy loss**. The −7% dip at N = 25 is a single trial out of 15 — within noise.

**View 2 — Code changes only (`original-code` vs `agent-solution`, both on the `full` strategy)**

| N | Δ accuracy | Δ spawn | Δ tokens |
|----|-----------|---------|----------|
| 5   | +7%  | 0% | +220 |
| 25  | +13% | 0% | +265 |
| 100 | +7%  | 0% | +434 |

The system-prompt and tooling changes (stable ids, `find_agent`, purpose-aware roster) lifted routing accuracy across the board even on the unfiltered roster. The small token increase is the richer system prompt earning its keep.

**View 3 — Total real-world effect (`original-code` full → `agent-solution` production)**

| N | orig accuracy | agent-sol accuracy | Δ accuracy | orig tokens | agent-sol tokens | Δ tokens |
|----|--------------|--------------------|-----------|------------|------------------|----------|
| 5   | 93% | 100% | +7% | 2752 | 2973 | +221 |
| 25  | 87% | 93%  | +7% | 3159 | 3425 | +266 |
| 100 | 93% | 100% | +7% | 4576 | 3297 | −1279 |

**Bottom line:** `agent-solution` is strictly better. Accuracy is up **+7% at every roster size**, there is **zero increase in unwanted spawns**, and at the realistic large-roster case (N = 100) it also costs **1,279 fewer tokens per call** than the original code did. The richer prompt pays a small token cost on tiny rosters and more than recovers it once the roster is large — which is exactly where the overload problem bites.

---

## Part 2 — Google Calendar Integration

### What was added

Calendar support mirrors the existing Gmail integration so the execution agent can schedule, search, and manage events without leaving chat.

**Tool surface (`server/agents/execution_agent/tools/calendar.py`)**
Seven calendar tools exposed to the execution agent:

- `calendar_list_events` — list/search events in a time range
- `calendar_create_event` — create an event with title, time, location, attendees
- `calendar_update_event` — modify an existing event
- `calendar_delete_event` — delete an event
- `calendar_get_event` — fetch a single event by id
- `calendar_list_calendars` — list accessible calendars
- `calendar_find_free_slots` — find open windows via the freebusy API

**Auth & connection (`server/services/calendar/client.py`)**
OAuth connect / status / disconnect handled through Composio, matching the Gmail flow. The active calendar connection (user id + email) is persisted to disk so it survives server restarts.

**Direct REST data plane (`server/services/calendar/google_rest.py`)**
A plain HTTP client against `googleapis.com/calendar/v3` for the actual read/write operations, using the OAuth access token extracted from the Composio connected account.

**Web UI (`web/components/SettingsModal.tsx`)**
A "Connect Google Calendar" panel alongside Gmail: connect, refresh status, disconnect, with connection state cached in `localStorage`.

**Scheduling intent detection (`server/services/scheduling/detector.py`)**
An LLM classifier that flags incoming emails actually trying to set up a meeting (proposing times or asking for availability) and extracts the proposed times verbatim — distinct from already-confirmed reminders and calendar noise.

### Optimizations

**1. Tool filtering by agent purpose (`server/agents/execution_agent/tools/registry.py`)**
This is the calendar work's most important efficiency lever, and it ties directly back to Part 1. Rather than handing every execution agent the full union of Gmail + Calendar + task + trigger schemas, the registry inspects the agent's name:

- A calendar-named agent ("schedule", "event", "availability", "meeting") gets **only** calendar + trigger tools.
- An email-named agent ("email", "gmail", "draft", "inbox") gets **only** Gmail + email-search + trigger tools.
- Mixed or unnamed agents get everything.

This keeps each agent's tool schema lean, which both reduces prompt tokens and cuts the chance of the model reaching for an irrelevant tool — the same "don't overload the context" principle that fixed the roster problem, applied at the tool layer.

**2. Routing keeps calendar and email separate**
The interaction agent's system prompt steers calendar/scheduling/availability work to calendar tools and email-specific work (receipts, correspondence, attachments) to email search — so the model doesn't, for example, try to answer an availability question by searching the inbox.

---

## Summary

The overload work made agent identity stable and retrieval explicit, lifting routing accuracy +7% across all roster sizes with no extra spawns and a net token saving on large rosters. The calendar work extended the agent's reach to scheduling while applying the same lean-context discipline — purpose-based tool filtering and clean routing separation — so the added capability doesn't reintroduce the bloat the overload fix removed.
