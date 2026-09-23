"""Read-only transcript search for approved remote agents."""

from agent_api.auth import classify_claims
from agent_api.errors import AgentError
from agent_api.identity import ensure_schema, note_speaker_change, reconcile
from agent_api.read import read_event
from agent_api.search import search_events

__all__ = [
    "AgentError",
    "classify_claims",
    "ensure_schema",
    "note_speaker_change",
    "read_event",
    "reconcile",
    "search_events",
]
