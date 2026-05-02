"""Request models for the receipt printer service."""
from typing import Any, Dict, List, Literal, Optional
from pydantic import BaseModel, Field


class SessionTicket(BaseModel):
    """A "Claude session completed" ticket — the workhorse format."""
    brand: str = Field("CLAUDE", min_length=1, max_length=40)
    title: str = Field(..., min_length=1, max_length=200)
    results: List[str] = Field(default_factory=list, max_length=20)
    duration: Optional[str] = Field(None, max_length=40)
    model: Optional[str] = Field(None, max_length=40)
    turns: Optional[int] = Field(None, ge=0, le=99999)
    timestamp: Optional[str] = Field(None, max_length=40,
                                     description="If omitted, server uses now.")


class TextRequest(BaseModel):
    """Free-form text to print verbatim. Newlines preserved."""
    text: str = Field(..., min_length=1, max_length=8000)
    cut: bool = True


class RichRequest(BaseModel):
    """A composed ticket: a list of typed rendering blocks.

    Blocks are validated permissively as plain dicts; the renderer is
    responsible for type-specific schema. Unknown block types fall
    back to a body-text rendering. See SKILL.md for the catalog.
    """
    blocks: List[Dict[str, Any]] = Field(..., min_length=1, max_length=80)


class SessionStatusUpdate(BaseModel):
    """Compact session-status update sent from a local hook to the Pi."""
    source: Literal["codex", "claude"]
    session_key: str = Field(..., min_length=1, max_length=200)
    turn_key: Optional[str] = Field(None, max_length=255)
    title: str = Field(..., min_length=1, max_length=200)
    summary_line: str = Field(..., min_length=1, max_length=240)
    status: Literal["running", "waiting", "completed", "blocked", "unknown"]
    cwd: Optional[str] = Field(None, max_length=500)
    model: Optional[str] = Field(None, max_length=80)
    turns: Optional[int] = Field(None, ge=0, le=99999)
    duration: Optional[str] = Field(None, max_length=40)
    updated_at: Optional[str] = Field(
        None, max_length=40, description="If omitted, server uses now."
    )
