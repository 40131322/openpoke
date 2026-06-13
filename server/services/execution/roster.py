"""Agent roster management - persists agent records with stable ids."""

import json
import time
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional

try:
    import fcntl
    _USE_FCNTL = True
except ImportError:
    import msvcrt
    _USE_FCNTL = False

from ...logging_config import logger


def _utc_now() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def _new_id() -> str:
    return uuid.uuid4().hex[:12]


class AgentRoster:
    """Roster that persists agent records (id, name, purpose, timestamps) as JSON."""

    def __init__(self, roster_path: Path):
        self._roster_path = roster_path
        self._records: List[Dict[str, Any]] = []
        self.load()

    def load(self) -> None:
        """Load roster from disk, migrating bare-string entries to full records."""
        if not self._roster_path.exists():
            self._records = []
            self.save()
            return
        try:
            with open(self._roster_path, "r", encoding="utf-8") as f:
                data = json.load(f)
            if not isinstance(data, list):
                self._records = []
                return
            migrated: List[Dict[str, Any]] = []
            dirty = False
            for entry in data:
                if isinstance(entry, str):
                    # Backward compat: bare string name → full record with fresh id
                    migrated.append({
                        "id": _new_id(),
                        "name": entry,
                        "purpose": "",
                        "created_at": _utc_now(),
                        "last_used": _utc_now(),
                    })
                    dirty = True
                elif isinstance(entry, dict) and entry.get("name"):
                    migrated.append(entry)
            self._records = migrated
            if dirty:
                self.save()
        except Exception as exc:
            logger.warning(f"Failed to load roster.json: {exc}")
            self._records = []

    def save(self) -> None:
        """Save records to disk with file locking."""
        max_retries = 5
        retry_delay = 0.1

        for attempt in range(max_retries):
            try:
                self._roster_path.parent.mkdir(parents=True, exist_ok=True)
                with open(self._roster_path, "w", encoding="utf-8") as f:
                    if _USE_FCNTL:
                        fcntl.flock(f.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
                        try:
                            json.dump(self._records, f, indent=2)
                            return
                        finally:
                            fcntl.flock(f.fileno(), fcntl.LOCK_UN)
                    else:
                        msvcrt.locking(f.fileno(), msvcrt.LK_NBLCK, 1)
                        try:
                            json.dump(self._records, f, indent=2)
                            return
                        finally:
                            f.seek(0)
                            msvcrt.locking(f.fileno(), msvcrt.LK_UNLCK, 1)
            except (BlockingIOError, OSError):
                if attempt < max_retries - 1:
                    time.sleep(retry_delay)
                    retry_delay *= 2
                else:
                    logger.warning("Failed to acquire lock on roster.json after retries")
            except Exception as exc:
                logger.warning(f"Failed to save roster.json: {exc}")
                break

    def add_agent(self, agent_name: str, purpose: Optional[str] = None) -> Dict[str, Any]:
        """Add an agent if the name is new; return the (existing or new) record."""
        existing = self.find_by_name(agent_name)
        if existing is not None:
            return existing
        now = _utc_now()
        record: Dict[str, Any] = {
            "id": _new_id(),
            "name": agent_name,
            "purpose": purpose or "",
            "created_at": now,
            "last_used": now,
        }
        self._records.append(record)
        self.save()
        return record

    def get_agents(self) -> List[str]:
        """Return agent names (backward-compatible)."""
        return [r["name"] for r in self._records]

    def get_records(self) -> List[Dict[str, Any]]:
        """Return all agent records."""
        return list(self._records)

    def find_by_id(self, agent_id: str) -> Optional[Dict[str, Any]]:
        """Return the record with the given id, or None."""
        for r in self._records:
            if r.get("id") == agent_id:
                return r
        return None

    def find_by_name(self, name: str) -> Optional[Dict[str, Any]]:
        """Return the record whose name matches exactly, or None."""
        for r in self._records:
            if r.get("name") == name:
                return r
        return None

    def touch(self, agent_id: str) -> None:
        """Update last_used timestamp for the given agent id."""
        for r in self._records:
            if r.get("id") == agent_id:
                r["last_used"] = _utc_now()
                self.save()
                break

    def clear(self) -> None:
        """Clear the agent roster."""
        self._records = []
        try:
            if self._roster_path.exists():
                self._roster_path.unlink()
            logger.info("Cleared agent roster")
        except Exception as exc:
            logger.warning(f"Failed to clear roster.json: {exc}")


_DATA_DIR = Path(__file__).resolve().parent.parent.parent / "data"
_ROSTER_PATH = _DATA_DIR / "execution_agents" / "roster.json"

_agent_roster = AgentRoster(_ROSTER_PATH)


def get_agent_roster() -> AgentRoster:
    """Get the singleton roster instance."""
    return _agent_roster
