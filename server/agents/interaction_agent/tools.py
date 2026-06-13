"""Tool definitions for interaction agent."""

import asyncio
import json
import math
import re
from collections import Counter
from dataclasses import dataclass
from typing import Any, Dict, List, Optional

from ...logging_config import logger
from ...services.conversation import get_conversation_log
from ...services.execution import get_agent_roster, get_execution_agent_logs
from ..execution_agent.batch_manager import ExecutionBatchManager


@dataclass
class ToolResult:
    """Standardized payload returned by interaction-agent tools."""

    success: bool
    payload: Any = None
    user_message: Optional[str] = None
    recorded_reply: bool = False


# Tool schemas for OpenRouter
TOOL_SCHEMAS = [
    {
        "type": "function",
        "function": {
            "name": "send_message_to_agent",
            "description": (
                "Deliver instructions to a specific execution agent. "
                "Reuses an existing agent when agent_id (preferred) or agent_name matches one in the roster; "
                "creates a new agent otherwise. Provide purpose when creating to improve future recall."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "agent_name": {
                        "type": "string",
                        "description": (
                            "Human-readable agent name (e.g., 'Email to Alice'). "
                            "Used as fallback when agent_id is not supplied."
                        ),
                    },
                    "instructions": {
                        "type": "string",
                        "description": "Instructions for the agent to execute.",
                    },
                    "agent_id": {
                        "type": "string",
                        "description": (
                            "Stable id shown in <active_agents>. "
                            "When provided, reuses that exact agent regardless of name."
                        ),
                    },
                    "purpose": {
                        "type": "string",
                        "description": (
                            "One-line description of what this agent handles. "
                            "Provide when creating a new agent to improve future recall."
                        ),
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
            "name": "find_agent",
            "description": (
                "Search for existing execution agents by natural-language query. "
                "Returns candidates with stable ids, names, purposes, and similarity scores. "
                "Use this to locate an agent before reusing it via send_message_to_agent."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "query": {
                        "type": "string",
                        "description": "What you're looking for (e.g., 'email to Alice', 'Bob project report').",
                    },
                },
                "required": ["query"],
                "additionalProperties": False,
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "send_message_to_user",
            "description": "Deliver a natural-language response directly to the user. Use this for updates, confirmations, or any assistant response the user should see immediately.",
            "parameters": {
                "type": "object",
                "properties": {
                    "message": {
                        "type": "string",
                        "description": "Plain-text message that will be shown to the user and recorded in the conversation log.",
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
                    "to": {
                        "type": "string",
                        "description": "Recipient email for the draft.",
                    },
                    "subject": {
                        "type": "string",
                        "description": "Email subject for the draft.",
                    },
                    "body": {
                        "type": "string",
                        "description": "Email body content (plain text).",
                    },
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
            "description": "Wait silently when a message is already in conversation history to avoid duplicating responses. Adds a <wait> log entry that is not visible to the user.",
            "parameters": {
                "type": "object",
                "properties": {
                    "reason": {
                        "type": "string",
                        "description": "Brief explanation of why waiting (e.g., 'Message already sent', 'Draft already created').",
                    },
                },
                "required": ["reason"],
                "additionalProperties": False,
            },
        },
    },
]

_EXECUTION_BATCH_MANAGER = ExecutionBatchManager()

_WORD = re.compile(r"[a-z0-9]+")


def _tokens(text: str) -> Counter:
    return Counter(_WORD.findall(text.lower()))


def _cosine(query: Counter, doc: str) -> float:
    d = _tokens(doc)
    if not d or not query:
        return 0.0
    dot = sum(query[t] * d[t] for t in query)
    nq = math.sqrt(sum(v * v for v in query.values()))
    nd = math.sqrt(sum(v * v for v in d.values()))
    return dot / (nq * nd) if nq and nd else 0.0


# Create or reuse execution agent (by id or name) and dispatch instructions asynchronously
def send_message_to_agent(
    agent_name: str,
    instructions: str,
    agent_id: Optional[str] = None,
    purpose: Optional[str] = None,
) -> ToolResult:
    """Send instructions to an execution agent, resolving identity by id first."""
    roster = get_agent_roster()
    roster.load()

    record = None

    # 1. Resolve by stable id (takes priority)
    if agent_id:
        record = roster.find_by_id(agent_id)
        if record is None:
            logger.warning(
                "agent_id not found; falling back to name lookup",
                extra={"agent_id": agent_id},
            )

    # 2. Resolve by exact name
    if record is None:
        record = roster.find_by_name(agent_name)

    # 3. Create new
    if record is None:
        record = roster.add_agent(agent_name, purpose=purpose)
        is_new = True
    else:
        is_new = False

    canonical_name = record["name"]
    roster.touch(record["id"])

    get_execution_agent_logs().record_request(canonical_name, instructions)

    action = "Created" if is_new else "Reused"
    logger.info(f"{action} agent: {canonical_name}")

    async def _execute_async() -> None:
        try:
            result = await _EXECUTION_BATCH_MANAGER.execute_agent(canonical_name, instructions)
            status = "SUCCESS" if result.success else "FAILED"
            logger.info(f"Agent '{canonical_name}' completed: {status}")
        except Exception as exc:  # pragma: no cover - defensive
            logger.error(f"Agent '{canonical_name}' failed: {str(exc)}")

    try:
        loop = asyncio.get_running_loop()
    except RuntimeError:
        logger.error("No running event loop available for async execution")
        return ToolResult(success=False, payload={"error": "No event loop available"})

    loop.create_task(_execute_async())

    return ToolResult(
        success=True,
        payload={
            "status": "submitted",
            "agent_name": canonical_name,
            "agent_id": record["id"],
            "new_agent_created": is_new,
        },
    )


# Search existing agents by query; returns up to 5 ranked candidates
def find_agent(query: str) -> ToolResult:
    """Return agents ranked by lexical similarity to the query."""
    roster = get_agent_roster()
    roster.load()
    records = roster.get_records()

    q = _tokens(query)
    scored: List[tuple] = [
        (r, _cosine(q, f"{r['name']} {r.get('purpose', '')}"))
        for r in records
    ]
    scored.sort(key=lambda x: x[1], reverse=True)

    candidates: List[Dict[str, Any]] = [
        {
            "id": r["id"],
            "name": r["name"],
            "purpose": r.get("purpose", ""),
            "score": round(s, 3),
        }
        for r, s in scored[:5]
        if s > 0
    ]

    return ToolResult(success=True, payload={"candidates": candidates})


# Send immediate message to user and record in conversation history
def send_message_to_user(message: str) -> ToolResult:
    """Record a user-visible reply in the conversation log."""
    log = get_conversation_log()
    log.record_reply(message)

    return ToolResult(
        success=True,
        payload={"status": "delivered"},
        user_message=message,
        recorded_reply=True,
    )


# Format and record email draft for user review
def send_draft(
    to: str,
    subject: str,
    body: str,
) -> ToolResult:
    """Record a draft update in the conversation log for the interaction agent."""
    log = get_conversation_log()

    message = f"To: {to}\nSubject: {subject}\n\n{body}"

    log.record_reply(message)
    logger.info(f"Draft recorded for: {to}")

    return ToolResult(
        success=True,
        payload={
            "status": "draft_recorded",
            "to": to,
            "subject": subject,
        },
        recorded_reply=True,
    )


# Record silent wait state to avoid duplicate responses
def wait(reason: str) -> ToolResult:
    """Wait silently and add a wait log entry that is not visible to the user."""
    log = get_conversation_log()
    log.record_wait(reason)

    return ToolResult(
        success=True,
        payload={
            "status": "waiting",
            "reason": reason,
        },
        recorded_reply=True,
    )


# Return predefined tool schemas for LLM function calling
def get_tool_schemas():
    """Return OpenAI-compatible tool schemas."""
    return TOOL_SCHEMAS


# Route tool calls to appropriate handlers with argument validation and error handling
def handle_tool_call(name: str, arguments: Any) -> ToolResult:
    """Handle tool calls from interaction agent."""
    try:
        if isinstance(arguments, str):
            args = json.loads(arguments) if arguments.strip() else {}
        elif isinstance(arguments, dict):
            args = arguments
        else:
            return ToolResult(success=False, payload={"error": "Invalid arguments format"})

        if name == "send_message_to_agent":
            return send_message_to_agent(**args)
        if name == "find_agent":
            return find_agent(**args)
        if name == "send_message_to_user":
            return send_message_to_user(**args)
        if name == "send_draft":
            return send_draft(**args)
        if name == "wait":
            return wait(**args)

        logger.warning("unexpected tool", extra={"tool": name})
        return ToolResult(success=False, payload={"error": f"Unknown tool: {name}"})
    except json.JSONDecodeError:
        return ToolResult(success=False, payload={"error": "Invalid JSON"})
    except TypeError as exc:
        return ToolResult(success=False, payload={"error": f"Missing required arguments: {exc}"})
    except Exception as exc:  # pragma: no cover - defensive
        logger.error("tool call failed", extra={"tool": name, "error": str(exc)})
        return ToolResult(success=False, payload={"error": "Failed to execute"})
