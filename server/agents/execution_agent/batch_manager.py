"""Coordinate execution agents and batch their results for the interaction agent."""

from __future__ import annotations

import asyncio
import uuid
from dataclasses import dataclass, field
from datetime import datetime
from typing import Dict, List, Optional

from .runtime import ExecutionAgentRuntime, ExecutionResult
from ...logging_config import logger


@dataclass
class PendingExecution:
    """Track a pending execution request."""

    request_id: str
    agent_name: str
    instructions: str
    turn_id: str
    created_at: datetime = field(default_factory=datetime.now)


@dataclass
class _BatchState:
    """Collect results for one interaction turn."""

    turn_id: str
    created_at: datetime = field(default_factory=datetime.now)
    pending: int = 0
    sealed: bool = False
    results: List[ExecutionResult] = field(default_factory=list)


class ExecutionBatchManager:
    """Run execution agents and deliver their combined outcome per turn."""

    def __init__(self, timeout_seconds: int = 90) -> None:
        self.timeout_seconds = timeout_seconds
        self._pending: Dict[str, PendingExecution] = {}
        self._batches: Dict[str, _BatchState] = {}
        self._batch_lock = asyncio.Lock()

    # ------------------------------------------------------------------ #
    # Turn lifecycle — called by the interaction runtime                   #
    # ------------------------------------------------------------------ #

    def begin_turn(self, turn_id: str) -> None:
        """Open a batch for this turn. Must be called synchronously before the loop."""
        if turn_id not in self._batches:
            self._batches[turn_id] = _BatchState(turn_id=turn_id)

    def register(
        self,
        turn_id: str,
        agent_name: str,
        instructions: str,
        request_id: str,
    ) -> None:
        """Register an agent into the batch synchronously before create_task.

        Called from send_message_to_agent (sync), so no await-point exists between
        multiple register() calls in one LLM iteration — the same-iteration race
        that caused fragmented batches is closed here.
        """
        state = self._batches.get(turn_id)
        if state is None:
            state = _BatchState(turn_id=turn_id)
            self._batches[turn_id] = state
        state.pending += 1
        self._pending[request_id] = PendingExecution(
            request_id=request_id,
            agent_name=agent_name,
            instructions=instructions,
            turn_id=turn_id,
        )

    async def seal_turn(self, turn_id: str) -> None:
        """Mark the turn as fully dispatched; triggers completion if all agents done.

        Called by the interaction runtime after _run_interaction_loop returns.
        If all registered agents already finished, dispatches immediately.
        If none were registered, discards the empty batch silently.
        """
        dispatch_payload: Optional[str] = None

        async with self._batch_lock:
            state = self._batches.get(turn_id)
            if state is None:
                return
            state.sealed = True
            if state.pending == 0:
                if state.results:
                    dispatch_payload = self._format_batch_payload(state.results)
                    names = [r.agent_name for r in state.results]
                    logger.info(f"Batch sealed (all done): {', '.join(names)}")
                del self._batches[turn_id]

        if dispatch_payload:
            await self._dispatch_to_interaction_agent(dispatch_payload)

    # ------------------------------------------------------------------ #
    # Direct execution — used by the trigger scheduler                    #
    # ------------------------------------------------------------------ #

    async def execute_agent(
        self,
        agent_name: str,
        instructions: str,
    ) -> ExecutionResult:
        """Self-contained single-agent execution for the trigger path.

        Forms its own one-agent batch, runs the agent, and dispatches the result
        to the interaction agent before returning.
        """
        turn_id = f"direct-{uuid.uuid4().hex[:8]}"
        request_id = str(uuid.uuid4())
        self.begin_turn(turn_id)
        self.register(turn_id, agent_name, instructions, request_id)

        result = await self._run_agent(agent_name, instructions, request_id)
        await self._complete_execution(turn_id, result)
        await self.seal_turn(turn_id)
        return result

    # ------------------------------------------------------------------ #
    # Internal execution helpers                                          #
    # ------------------------------------------------------------------ #

    async def _run_agent(
        self,
        agent_name: str,
        instructions: str,
        request_id: str,
    ) -> ExecutionResult:
        """Run the execution agent runtime with timeout and error handling."""
        try:
            logger.info(f"[{agent_name}] Execution started")
            runtime = ExecutionAgentRuntime(agent_name=agent_name)
            result = await asyncio.wait_for(
                runtime.execute(instructions),
                timeout=self.timeout_seconds,
            )
            status = "SUCCESS" if result.success else "FAILED"
            logger.info(f"[{agent_name}] Execution finished: {status}")
            return result
        except asyncio.TimeoutError:
            logger.error(f"[{agent_name}] Execution timed out after {self.timeout_seconds}s")
            return ExecutionResult(
                agent_name=agent_name,
                success=False,
                response=f"Execution timed out after {self.timeout_seconds} seconds",
                error="Timeout",
            )
        except Exception as exc:  # pragma: no cover - defensive
            logger.exception(f"[{agent_name}] Execution failed unexpectedly")
            return ExecutionResult(
                agent_name=agent_name,
                success=False,
                response=f"Execution failed: {exc}",
                error=str(exc),
            )
        finally:
            self._pending.pop(request_id, None)

    async def _complete_execution(self, turn_id: str, result: ExecutionResult) -> None:
        """Record a result and dispatch if the batch is sealed and all agents done."""
        dispatch_payload: Optional[str] = None

        async with self._batch_lock:
            state = self._batches.get(turn_id)
            if state is None:
                logger.warning(f"Dropping result for unknown turn {turn_id}")
                return
            state.results.append(result)
            state.pending -= 1
            if state.pending == 0 and state.sealed:
                dispatch_payload = self._format_batch_payload(state.results)
                names = [r.agent_name for r in state.results]
                logger.info(f"Batch complete for turn {turn_id}: {', '.join(names)}")
                del self._batches[turn_id]

        if dispatch_payload:
            await self._dispatch_to_interaction_agent(dispatch_payload)

    def get_pending_executions(self) -> List[Dict]:
        """Expose pending executions for observability."""
        return [
            {
                "request_id": p.request_id,
                "agent_name": p.agent_name,
                "turn_id": p.turn_id,
                "created_at": p.created_at.isoformat(),
                "elapsed_seconds": (datetime.now() - p.created_at).total_seconds(),
            }
            for p in self._pending.values()
        ]

    async def shutdown(self) -> None:
        """Clear pending bookkeeping (no background work remains)."""
        self._pending.clear()
        async with self._batch_lock:
            self._batches.clear()

    def _format_batch_payload(self, results: List[ExecutionResult]) -> str:
        entries: List[str] = []
        for result in results:
            status = "SUCCESS" if result.success else "FAILED"
            response_text = (result.response or "(no response provided)").strip()
            entries.append(f"[{status}] {result.agent_name}: {response_text}")
        return "\n".join(entries)

    async def _dispatch_to_interaction_agent(self, payload: str) -> None:
        from ..interaction_agent.runtime import InteractionAgentRuntime

        runtime = InteractionAgentRuntime()
        try:
            loop = asyncio.get_running_loop()
        except RuntimeError:
            asyncio.run(runtime.handle_agent_message(payload))
            return

        loop.create_task(runtime.handle_agent_message(payload))


_batch_manager = ExecutionBatchManager()


def get_execution_batch_manager() -> ExecutionBatchManager:
    return _batch_manager
