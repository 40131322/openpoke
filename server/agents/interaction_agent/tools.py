"""Tool definitions for interaction agent."""

import asyncio
import json
import uuid
from dataclasses import dataclass
from typing import Any, Optional

from ...logging_config import logger
from ...services.conversation import get_conversation_log
from ...services.execution import get_execution_agent_logs
from ...services.execution.registry import get_agent_registry
from ..execution_agent.batch_manager import get_execution_batch_manager


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
                "Reuses an existing agent when the name (or agent_id) matches one in the roster; "
                "creates a new agent otherwise. Provide purpose when creating to improve future recall."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "agent_name": {
                        "type": "string",
                        "description": (
                            "Human-readable agent name describing its purpose "
                            "(e.g., 'Vercel Job Offer', 'Email to Sharanjeet'). "
                            "Used to identify and potentially reuse the agent."
                        ),
                    },
                    "instructions": {
                        "type": "string",
                        "description": "Instructions for the agent to execute.",
                    },
                    "agent_id": {
                        "type": "string",
                        "description": (
                            "Stable agent ID returned by find_agent. "
                            "When provided, reuses that exact agent regardless of name."
                        ),
                    },
                    "purpose": {
                        "type": "string",
                        "description": (
                            "One-line description of what this agent does. "
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
                "Use this to recall agents outside the active list before reusing them via send_message_to_agent."
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

def send_message_to_agent(
    agent_name: str,
    instructions: str,
    turn_id: str = "",
    agent_id: Optional[str] = None,
    purpose: str = "",
) -> ToolResult:
    """Send instructions to an execution agent, reusing one if a match is found.

    Registration into the batch is synchronous (happens before create_task) so
    every agent dispatched in one LLM iteration is counted before any of them run.
    """
    registry = get_agent_registry()
    registry.load()
    record, created = registry.resolve_or_create(
        agent_id=agent_id,
        display_name=agent_name,
        purpose=purpose,
    )

    get_execution_agent_logs().record_request(record.display_name, instructions)

    action = "Created" if created else "Reused"
    logger.info(f"{action} agent: {record.display_name} (id={record.id})")

    try:
        loop = asyncio.get_running_loop()
    except RuntimeError:
        logger.error("No running event loop available for async execution")
        return ToolResult(success=False, payload={"error": "No event loop available"})

    request_id = uuid.uuid4().hex

    # Register synchronously before create_task — pending count is set before any task runs.
    mgr = get_execution_batch_manager()
    mgr.register(turn_id, record.display_name, instructions, request_id)

    async def _run_task() -> None:
        try:
            result = await mgr._run_agent(record.display_name, instructions, request_id)
            await mgr._complete_execution(turn_id, result)
            status = "SUCCESS" if result.success else "FAILED"
            logger.info(f"Agent '{record.display_name}' completed: {status}")
        except Exception as exc:  # pragma: no cover - defensive
            logger.error(f"Agent '{record.display_name}' task failed: {str(exc)}")

    loop.create_task(_run_task())

    return ToolResult(
        success=True,
        payload={
            "status": "submitted",
            "agent_id": record.id,
            "agent_name": record.display_name,
            "new_agent_created": created,
        },
    )


def find_agent(query: str) -> ToolResult:
    """Search active agents by query; return top candidates with id, name, purpose, score."""
    registry = get_agent_registry()
    registry.load()
    candidates = registry.find_candidates(query, limit=5)
    results = [
        {
            "id": r.id,
            "name": r.display_name,
            "purpose": r.purpose,
            "score": round(s, 3),
        }
        for r, s in candidates
    ]
    return ToolResult(
        success=True,
        payload={"candidates": results, "count": len(results)},
    )


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


def get_tool_schemas():
    """Return OpenAI-compatible tool schemas."""
    return TOOL_SCHEMAS


def handle_tool_call(name: str, arguments: Any, turn_id: str = "") -> ToolResult:
    """Handle tool calls from interaction agent."""
    try:
        if isinstance(arguments, str):
            args = json.loads(arguments) if arguments.strip() else {}
        elif isinstance(arguments, dict):
            args = arguments
        else:
            return ToolResult(success=False, payload={"error": "Invalid arguments format"})

        if name == "send_message_to_agent":
            return send_message_to_agent(
                agent_name=args.get("agent_name", ""),
                instructions=args.get("instructions", ""),
                turn_id=turn_id,
                agent_id=args.get("agent_id"),
                purpose=args.get("purpose", ""),
            )
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
