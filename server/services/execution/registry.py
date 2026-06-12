"""Agent registry — stable-id records with two-tier lookup and archiving."""

from __future__ import annotations

import json
import re
import threading
import time
import uuid
from dataclasses import dataclass, asdict, field
from pathlib import Path
from typing import Optional

from ...logging_config import logger

REUSE_THRESHOLD = 0.6
HOT_N = 20  # max records rendered in the interaction-agent prompt


def _normalize(name: str) -> str:
    slug = "".join(c if c.isalnum() else "-" for c in name.strip().lower())
    return re.sub(r"-+", "-", slug).strip("-") or "agent"


@dataclass
class AgentRecord:
    id: str
    display_name: str
    purpose: str
    norm_key: str
    status: str = "active"
    created_at: float = field(default_factory=time.time)
    last_active: float = field(default_factory=time.time)

    @staticmethod
    def new(display_name: str, purpose: str = "") -> "AgentRecord":
        return AgentRecord(
            id=uuid.uuid4().hex[:12],
            display_name=display_name,
            purpose=purpose,
            norm_key=_normalize(display_name),
        )


def _score(query: str, rec: AgentRecord) -> float:
    """Token-overlap score over name+purpose. Swap for embedding cosine later."""
    q = set(_normalize(query).split("-"))
    hay = set(_normalize(f"{rec.display_name} {rec.purpose}").split("-"))
    return len(q & hay) / len(q) if q else 0.0


class AgentRegistry:
    def __init__(self, path: Path):
        self._path = path
        self._lock = threading.Lock()
        self._by_id: dict[str, AgentRecord] = {}
        self.load()

    def load(self) -> None:
        with self._lock:
            self._by_id = {}
            if not self._path.exists():
                return
            try:
                raw = json.loads(self._path.read_text(encoding="utf-8"))
            except Exception as exc:
                logger.warning(f"Failed to load agent registry: {exc}")
                return
            if isinstance(raw, list):
                # Migrate old bare-list roster.json
                for name in raw:
                    rec = AgentRecord.new(str(name))
                    self._by_id[rec.id] = rec
                self._save_locked()
                return
            for d in raw.get("agents", []):
                try:
                    rec = AgentRecord(**d)
                    self._by_id[rec.id] = rec
                except Exception as exc:
                    logger.warning(f"Skipping malformed agent record: {exc}")

    def _save_locked(self) -> None:
        self._path.parent.mkdir(parents=True, exist_ok=True)
        tmp = self._path.with_suffix(".tmp")
        tmp.write_text(
            json.dumps({"agents": [asdict(r) for r in self._by_id.values()]}, indent=2),
            encoding="utf-8",
        )
        tmp.replace(self._path)

    def get(self, agent_id: str) -> Optional[AgentRecord]:
        return self._by_id.get(agent_id)

    def touch(self, agent_id: str) -> None:
        with self._lock:
            rec = self._by_id.get(agent_id)
            if rec:
                rec.last_active = time.time()
                self._save_locked()

    def active(self) -> list[AgentRecord]:
        return [r for r in self._by_id.values() if r.status == "active"]

    def find_candidates(self, query: str, *, limit: int = 5) -> list[tuple[AgentRecord, float]]:
        """Two-tier lookup: exact norm_key match first, then scored relevance."""
        nkey = _normalize(query)
        exact = [r for r in self.active() if r.norm_key == nkey]
        if exact:
            return [(exact[0], 1.0)]
        scored = [(r, _score(query, r)) for r in self.active()]
        scored = [(r, s) for r, s in scored if s > 0.0]
        scored.sort(key=lambda x: x[1], reverse=True)
        return scored[:limit]

    def resolve_or_create(
        self,
        *,
        agent_id: Optional[str] = None,
        display_name: str,
        purpose: str = "",
    ) -> tuple[AgentRecord, bool]:
        """Return (record, created). Reuses before spawning."""
        if agent_id and (rec := self.get(agent_id)):
            self.touch(rec.id)
            return rec, False
        top = self.find_candidates(display_name, limit=1)
        if top and top[0][1] >= REUSE_THRESHOLD:
            rec = top[0][0]
            self.touch(rec.id)
            return rec, False
        rec = AgentRecord.new(display_name, purpose)
        with self._lock:
            self._by_id[rec.id] = rec
            self._save_locked()
        return rec, True

    def archive_idle(self, *, idle_seconds: float, pinned: set[str]) -> int:
        """Mark agents inactive for idle_seconds as archived, skipping pinned ids."""
        cutoff = time.time() - idle_seconds
        n = 0
        with self._lock:
            for rec in self._by_id.values():
                if rec.status == "active" and rec.id not in pinned and rec.last_active < cutoff:
                    rec.status = "archived"
                    n += 1
            if n:
                self._save_locked()
        return n

    def clear(self) -> None:
        with self._lock:
            self._by_id = {}
            try:
                if self._path.exists():
                    self._path.unlink()
            except Exception as exc:
                logger.warning(f"Failed to delete registry file: {exc}")


_DATA_DIR = Path(__file__).resolve().parent.parent.parent / "data"
_REGISTRY_PATH = _DATA_DIR / "execution_agents" / "registry.json"

_agent_registry = AgentRegistry(_REGISTRY_PATH)


def get_agent_registry() -> AgentRegistry:
    return _agent_registry
